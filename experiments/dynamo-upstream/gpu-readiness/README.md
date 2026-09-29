# Combined run controls

The [$5 plan](../combined-gpu-plan.md) and local cleanup substitute are approved. The [first readiness attempt](gpu-attempt-01/README.md) failed the container capture-permission gate. The pod was deleted before any model download or inference.

`create_once.py` checks the frozen readiness files, account balance and current GPU quote. It arms a detached reconciliation guard, creates one pod and starts a separate cleanup controller, and records the allocation time before sending the request. An uncertain response is reconciled by the unique pod name. It never retries creation. The dedicated SSH key and Runpod key stay outside the repository.

The watchdog closes admission at minute 130 and deletes by minute 140. Missing readiness at minute 35 also triggers deletion. The controller deletes on exit. Both verify disappearance from the pod list. These local processes need the Mac to remain powered and connected; that limitation was included in the user's approval.

`remote.py` uses the dedicated SSH key. `package_run.py` packs the matched wheels, scripts and public fixtures. `export_evidence.py` copies hashed snapshots to the Mac during the run. The final export is made after the capture processes stop.

## CPU checks

The control and KV collector tests currently pass 25 checks:

```bash
python3 -m unittest discover -s experiments/dynamo-upstream/gpu-readiness -p 'test_*.py'
```

The [KV wire check](kv-wire-native/result.json) encoded classes from the pinned vLLM source and sent three batches over local ZMQ. The collector kept the original frames and reconstructed the expected block set. This checks serialization and collection. GPU allocation and live cache behavior still need the approved run.

The [metric sources](metric-source/manifest.json) define `vllm:prompt_tokens_by_source_total{source="local_compute"}`. Its delta separates locally computed prompt tokens from cache hits. Per-request attribution also needs an unchanged backend identity and no overlapping requests. Raw metric snapshots remain part of the evidence.

The [control-plane record](control-plane/README.md) explains why the original provider timer could not be used. [approval.json](approval.json) records the accepted replacement and remaining publication restrictions.
