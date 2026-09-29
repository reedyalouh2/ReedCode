# Linux build readiness

The pinned backend image's Dynamo wheels import on a local Linux CPU container. Its HTTP frontend also accepts the captured Claude and Codex requests and sends rendered token IDs through Dynamo's TCP transport to a capture worker.

Both matched main-revision Linux wheels are built, hashed, and checked through the real HTTP frontend. The installed fix sends a 192-token tool warmup that exactly prefixes its 218-token continuation, with thinking disabled. These CPU checks clear the artifact gate; the live engine, reset, and capture checks remain.

| CPU check | Result |
| --- | --- |
| Image manifest and extracted wheel hashes | Passed |
| Shipped Dynamo 1.5.0 runtime and frontend imports | Passed, with the image's FFmpeg libraries |
| Claude request 1, 22 tools | HTTP 200; 19,975 input tokens |
| Claude request 2, tool history | HTTP 200; 20,111 input tokens |
| Claude input plus 8,192 output allowance | 28,167 and 28,303; both fit 32,768 |
| Codex request 1, 8 tool entries | HTTP 200; 8,696 input tokens |
| Codex request 2, tool history | HTTP 200; 8,869 input tokens |
| Matched stock/fixed main Linux runtime wheels | Built; both import |
| Main frontend with image's 1.5.0 backend runtime | Both pass TCP transport; normal token IDs match |
| Installed hint controls | Off sends no warmup; fixed text skips; fixed tool prepares an exact prefix |

## What ran

I extracted the Dynamo wheelhouse and FFmpeg libraries from `nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e`. Both layer digests and individual file hashes are recorded. The full CUDA image was not pulled.

The CPU check used those exact Dynamo wheels and FFmpeg libraries in an x86_64 Docker container on the Mac. Python 3.11 and Debian Bookworm differ from the image's Python 3.12 and Ubuntu 24.04. The frontend used its normal Rust preprocessor, the pinned Qwen3 tokenizer, `hermes` tool parsing, `qwen3` reasoning parsing, and thinking enabled. The Claude preamble stayed intact.

The worker registered through the shipped runtime's public API. It saved the received request and returned three fixed tokens. This checks HTTP handling, rendering, and transport. It does not run vLLM or a model. The callback's JSON capture also does not prove that stock vLLM logs expose token IDs: the Python ingress trace prints `PythonPayload(<PyAny>)`.

The first attempt failed during model discovery because the emulated, network-disabled container's IP resolver returned a Netlink error. The documented `DYN_TCP_RESPONSE_STREAM_HOST=lo` setting resolved it. Those failed logs are retained.

Claude's raw request contains a 4,096-token thinking budget. The normalized backend `max_thinking_tokens` is `null`, so this check makes no claim that the budget is enforced. Codex sends no output-token limit; its inputs fit the context, but an input-plus-output budget check is still needed.

## Matched builds

Both builds start from `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`. The fixed copy adds only the two reviewed patches under `../fix/`. They share Rust 1.96.1, a release profile, the normal Linux binding features, the image's FFmpeg libraries, and `CUDARC_CUDA_VERSION=13000`. CUDA and NIXL use the build's dynamic-loading paths; GPU functionality remains untested.

The isolated build tree is `/tmp/reedcode-linux-readiness`. The build ran in `reedcode-linux-build-main`: 55m 22s for stock and 24m 16s for fixed under x86 emulation. Its build cache can be reused. Original source checkouts and their build targets were left alone. The three wheels and their manifest are under `/tmp/reedcode-linux-readiness/artifacts/`.

`Dockerfile` and `build-pair.sh` record the environment and build commands. Stock uses the pinned lockfile. The lock audit checked all 945 package entries. Only the renderer registry entry changes to the reviewed local path. Both imports and cross-version TCP checks passed. The initial fixed smoke incorrectly expected a text warmup; that assertion was corrected to match the documented tool-only scope, and its failure log was retained.

## Pod handoff

`setup-frontends.sh BUNDLE_DIR STUDY_DIR` verifies the wheel hashes and creates separate frontend virtual environments with `--system-site-packages` and `--no-deps`. The image's Dynamo 1.5.0 backend stays in the base environment. `launch-on-pod.sh STUDY_DIR prefill|parity backend|image|stock|fixed` contains the reviewed commands. Neither script has been executed on a pod here.

The launcher pins Qwen's revision, BF16 weights with automatic KV dtype, TP1, 16-token blocks, and a 32,768-token context. Prefill uses thinking disabled; parity uses thinking enabled and the image frontend. Backend RPC/response ports are 20000/20001; frontend RPC/response ports are 20002/20003. The KV publisher binds `tcp://*:5557`; its local collector connects to `127.0.0.1:5557`.

Save the actual engine allocation configuration and confirm full attention plus 16-token event/hash blocks before interpreting cache events. CLI flags alone do not establish those facts. A reset also needs a live clear event and a successful current-frontend trace with `global_radix_tree_size=0`. The pinned trace source and hash are under `sources/`.

`hooks.template.json` supplies the coordinator's two argv hooks for `/tmp/reedcode-bundle/repo` and `/tmp/reedcode-study`. Seed `prefill/frontend-state.json` after launching the initial frontend, recording its `frontend_pid`, Linux `/proc` `frontend_start_ticks`, and a unique `frontend_epoch`. The hook refuses to stop a process whose identity or module differs. Linux pidfds keep its signals bound to that process.

Each fresh frontend gets a unique stdout log and request-trace file. After model discovery, the hook resets the worker, sends the one-token canary, and requires a current successful clear event with a zero-sized router tree. It returns the coordinator's proof path and hash. Eight local unit checks cover ownership, PID reuse, reset port isolation, proof filtering, success, and cleanup on failure. A live GPU reset remains a deployment gate.

The native Mac KV-event encoding and ZMQ check passed under `../../gpu-readiness/kv-wire-native/`. Two local x86-emulated attempts stalled before the collector started; their logs are retained. No further emulation retries were made.

## Evidence

- `backend-wheel-evidence.json` and `backend-ffmpeg-evidence.json`: exact image layers and extracted file hashes.
- `backend-import.log`: shared-library resolution and import result.
- `stock-protocol-summary.json`: Claude request hashes, token counts, and settings.
- `stock-codex-protocol-summary.json`: Codex request hashes, token counts, and settings.
- `stock-protocol-evidence.tar.gz` and `stock-codex-protocol-evidence.tar.gz`: raw logs, callback captures, responses, and the scripts used.
- `build-manifest.json`, `source-lock-audit.json`, and `artifact-manifest.json`: source, dependency, build, and wheel hashes.
- `installed-runtime-summary.json` and `installed-runtime-evidence.tar.gz`: installed warmup controls, explicit disabled-thinking checks, and both main frontends using the image runtime.

No paid resource was created. No artifact was uploaded or pushed.
