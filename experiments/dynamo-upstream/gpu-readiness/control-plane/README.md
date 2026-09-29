# Runpod readiness check

The inspected CLI was **runpodctl 2.14.0**. Its `pod create` help has no `--terminate-after` or `--stop-after` flag. The public API schema has no pod scheduling field.

[PR #330](https://github.com/runpod/runpodctl/pull/330) removed those flags because the backend accepted their values without stopping or deleting the pod. [PR #331](https://github.com/runpod/runpodctl/pull/331), which would restore the flags, was open and marked draft at inspection. Its description says the backend must enforce the timer before it can merge. These were read on September 28, 2026 (September 29 UTC).

The [manifest](manifest.json) hashes the public API responses, API schema, local version and help output. These fetches used no credentials. The original combined plan's provider-timer requirement could not be met through these interfaces.

The completed study used local deadline and cleanup scripts. The pod was deleted and account spend returned to zero. [Run record](../../combined-gpu-20260929/operations/billing.json).
