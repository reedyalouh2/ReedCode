# Capture and deployment checks

The first rental failed packet-capture permissions and ended before model download. A runtime-user-only image change enabled capture; all filesystem layers stayed identical. [Image proof](runpod-retry-proposal/image/README.md).

`package_run.py` packs matched frontend wheels, scripts and public fixtures. `export_evidence.py` saves hashed snapshots, with the final export taken after capture processes stop. `remote.py` checks complete transfer hashes. The [completed study](../combined-gpu-20260929/README.md) includes the resulting archive.

## CPU checks

The control and KV collector tests currently pass 25 checks:

```bash
python3 -m unittest discover -s experiments/dynamo-upstream/gpu-readiness -p 'test_*.py'
```

The [KV wire check](kv-wire-native/result.json) encoded classes from the pinned vLLM source and sent three batches over local ZMQ. The collector kept the original frames and reconstructed the expected block set. This checks serialization and collection. The [completed GPU run](../combined-gpu-20260929/README.md) supplies the live cache evidence.

The [metric sources](metric-source/manifest.json) define `vllm:prompt_tokens_by_source_total{source="local_compute"}`. Its delta separates locally computed prompt tokens from cache hits. Per-request attribution also needs an unchanged backend identity and no overlapping requests. Raw metric snapshots remain part of the evidence.
