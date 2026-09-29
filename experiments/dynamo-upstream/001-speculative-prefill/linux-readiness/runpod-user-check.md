# Container user readiness

Runpod's treatment of this image's `USER dynamo` remains unverified. I found no documented container-user override in the checked create/update API schema or CLI. This is an immediate readiness gate before downloading the model or starting either study.

The current [create-Pod documentation](https://docs.runpod.io/api-reference/pods/POST/pods) documents entrypoint and command overrides. The [CLI v2.14.0 source](https://github.com/runpod/runpodctl/blob/v2.14.0/cmd/pod/create.go#L519) parses `--docker-args` as the container command or a JSON command/entrypoint object. It is not a Docker engine option passthrough; using `--user` there would be unsupported.

The locally saved v2 OpenAPI schema resolves `CreatePodRequest` to `args`, `cloud`, `cmd`, `cpu`, `dataCenterIds`, `disk`, `entrypoint`, `env`, `globalNetworking`, `gpu`, `image`, `mounts`, `name`, `ports`, `registry`, `startJupyter`, `startSsh`, and `templateId`. No user or capability override appears. A fresh unauthenticated fetch of the schema returned HTTP 403, so this list describes the saved schema rather than a newly fetched version.

The previous deployment's saved `server-records.tar.gz` contains no UID or capability snapshot. A successful prior SSH connection does not establish the privileges needed by this capture process.

On first connection, record `id`, `Uid` and capability fields from `/proc/self/status`, and whether `tcpdump` is installed. Open a bounded loopback capture with the actual study filter and verify a generated local request produces packets. Root UID alone is insufficient evidence for capture permission. If install or capture is denied, stop at this gate; do not alter the pinned image or attempt a privilege workaround.

Evidence hashes:

- `/tmp/reedcode-runpod-openapi.json`: `e6dc0833b4be94330cff33b5b611993f2fa0bf67327c7dea74b28b2b0c5db31e`
- `/tmp/reedcode-runpodctl-v2.14.0/cmd/pod/create.go`: `9bd4c614ed35d3aad98754198fac59411aff6ab8c7c77d42dbba1bf2dcd972db`

No pod was created and no image was changed during this check.
