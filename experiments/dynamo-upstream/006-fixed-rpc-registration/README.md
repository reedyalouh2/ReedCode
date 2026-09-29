# Fixed TCP RPC port and frontend registration

**Status: local startup failure observed; a new upstream bug is unconfirmed.** Pinned main already changed this path. Search existing issues and identify that change before considering a report.

The stock 1.5 image frontend failed to register Qwen when `DYN_TCP_RPC_PORT` was set. The service kept retrying model discovery; Chat and Completions returned empty 404 responses. Unsetting the fixed RPC port for that frontend allowed registration and the later canary and client runs.

The inspected v1.5.0 source skips request-plane initialization when a fixed port exists. Its subsequent `Option::unwrap_or(get_actual_tcp_rpc_port()?)` evaluates the fallback eagerly. That lookup fails before the global bound port has been initialized. Main initializes the TCP request-plane server before constructing the transport address.

- [Saved startup logs](evidence/frontend-registration-readonly.json) and [diagnosis](evidence/frontend-startup-diagnosis.json).
- [v1.5.0 endpoint source](evidence/frontend-startup-release-endpoint.rs), revision `b83b1d9304ebfc624709ac46db32b1b6f1ff1615`, lines 331–335 and 369–380.
- [Main endpoint source](evidence/frontend-startup-main-endpoint.rs), revision `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, lines 364–369.
- [Source and evidence hashes](manifest.json).

The saved diagnosis records what was known during startup, including checks still pending at that moment. It has been preserved unchanged. The source comparison uses the release tag and pinned main; the runtime image reports its own commit, `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`.

Next: make an isolated CPU reproduction, search existing issues and PRs, and determine whether main's change already covers the failure. No issue or fix has been filed. This startup case is separate from the later HTTP 503 returned by our measurement identity sidecar before any prefill replay model request was sent.
