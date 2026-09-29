# Packet-capture deployment

The first pod ran the pinned image as UID 1000 with no effective capture capabilities or tcpdump. It ended before model download. The checked Runpod interfaces had no runtime-user override.

The replacement image changes only the OCI runtime user to root. Every filesystem layer matches the recorded Dynamo 1.5.0 parent. The [image proof](image/proof.json) records both digests and the exact config change.

The [parent CPU check](image/cpu-validation/cpu-run.json) and [derived-image check](image/derived-cpu-validation/evidence-manifest.json) verified package installation and loopback capture. Python package versions remained unchanged. The replacement then completed the [GPU study](../../combined-gpu-20260929/README.md).
