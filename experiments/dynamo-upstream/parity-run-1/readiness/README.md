# Client readiness

Both pinned clients read a local fixture through their normal coding tools and resumed the same session twice. These checks used scripted local responses. They made **zero model requests** and used no GPU.

| Check | Claude Code 2.1.81 | Codex CLI 0.155.1 |
| --- | --- | --- |
| Request path | `/v1/messages?beta=true` | `/v1/responses` |
| Model | `Qwen/Qwen3-8B` | `Qwen/Qwen3-8B` |
| Tool entries in first request | 22 | 8 |
| Reasoning requested | `enabled`, budget 4,096 | `medium`, summary `auto` |
| Output limit in request | 8,192 | Field absent |
| Successful local tool | `Read` | `exec_command` |
| Real resumed follow-ups | 2 | 2 |
| Total inference-shaped requests to stub | 4 | 4 |
| Request bytes preserved; streamed answer received | Passed | Passed |

The [Claude capture](verified/claude/result.json) includes the stock `erased` reasoning signature. The client accepted it and returned reasoning alongside the tool result. The [Codex capture](coding-tools-seccomp/codex/result.json) includes an actual successful `cat fixture.txt` call under its `workspace-write` sandbox. Each session executed one tool; its result then appeared in three request histories. The older Claude summary calls those three appearances `tool_results_returned`.

The stub's token usage is invented test data. Its answers say nothing about model quality, cache reuse, or GPU compatibility. The [stock frontend check](../../001-speculative-prefill/linux-readiness/stock-protocol-summary.json) is a separate CPU test using the real pinned Dynamo frontend. The final readiness step is a real streamed smoke response from each client on the configured server.

## Capture and transport

`capture_proxy.py` forwards HTTP entity bodies without parsing or rewriting JSON or SSE. HTTP transfer framing and hop-by-hop headers can change. It saves the exact request and response bodies, their SHA-256 hashes, safe headers, response status, and chunk receipt times. `complete` means that the HTTP response body finished; inspect SSE events and stop reasons separately.

Only the dummy `Bearer local-dynamo` authorization value is admitted. Other credentials are rejected before the body is read or forwarded. Header values containing credentials are never recorded. The proxy admits at most 15 inference requests across all three turns, and ends in-flight reads at its shared 1,500-second deadline. Rejected requests stay in the ledger. An outer watchdog must also stop the client and its tools at 25 minutes.

All [twelve proxy tests passed](proxy-tests.txt). They cover byte preservation, incremental streaming, chunked requests, complete and truncated fixed-length responses, the 15-request boundary, credential rejection, duplicate authorization, tracing headers, the time limit, and partial capture when the controller stops a request.

The measured study enables `--trace-id-prefix`. When a client supplies no `X-Request-ID`, the recorder adds a unique observer ID and saves it separately from the original headers. Existing client IDs stay unchanged. The stock frontend’s `request received` row links that ID to its internal request ID, which matches the decoded TCP control ID. The [four-request stock check](../wire-capture/README.md) verified this linkage and identical token arrays with and without observer headers. Save those exact log rows with the wire evidence.

The [Docker transport check](transport-verified.json) also passed. It sent one fixed request through the container's loopback proxy and TCP relay to a temporary Mac server bound to `127.0.0.1`. Both body hashes matched and two response chunks arrived separately. For the GPU session, the host endpoint will be the SSH forward on port **18002**. `transport_relay.py` connects only to `host.docker.internal`; it exposes its listening socket only on container loopback.

## Reproduce the CPU checks

From the repository root, use a new output directory on each run:

```sh
python3 -m unittest discover \
  -s experiments/dynamo-upstream/parity-run-1/readiness \
  -p 'test_capture_proxy.py' -v

python3 experiments/dynamo-upstream/parity-run-1/readiness/check_clients.py \
  --client claude --claude /Users/mohamed/.local/bin/claude \
  --output /tmp/parity-claude-check --timeout 60
```

The Claude check creates its own temporary task and configuration. A macOS sandbox blocks external network, personal config contents, keychain access, and writes outside that task. It also denies the resolved study credential directories and common cloud credential stores. A [dummy-file test](profile-tests.txt) checked that the task remained readable while a denied credential fixture could not be read. No real secret was read. The client keeps its normal prompt and tool schemas. Only the fixture read is pre-authorized in this CPU check.

For Codex, obtain the pinned ARM Linux asset recorded in [provenance.json](provenance.json), verify its hash, and extract it outside the repository. Build the local test image with `Dockerfile.native`. The successful command was:

```sh
PARITY_READY="$PWD/experiments/dynamo-upstream/parity-run-1/readiness"

docker build --platform linux/arm64 \
  -f "$PARITY_READY/Dockerfile.native" \
  -t reedcode-codex-stub-native:0.155.1 "$PARITY_READY"

docker run --rm --network none --platform linux/arm64 \
  --cap-add SYS_ADMIN \
  --security-opt "seccomp=$PARITY_READY/container/seccomp-codex.json" \
  --tmpfs /root --tmpfs /tmp \
  -v /tmp/reedcode-parity-codex-linux-arm64/codex-aarch64-unknown-linux-musl:/codex:ro \
  -v "$PARITY_READY:/readiness:ro" \
  -v "$PARITY_READY:/evidence" \
  reedcode-codex-stub-native:0.155.1 \
  python3 /readiness/check_clients.py --client codex --codex /codex \
  --output /evidence/coding-tools-seccomp-repeat --timeout 60 --container-isolated
```

This container has no personal mounts or external network. Its ordinary `/root` and `/tmp` directories are temporary. No `HOME` or `CODEX_HOME` override is used. Codex's sandbox needed namespace capability and `pivot_root`; the pinned [seccomp profile](container/manifest.json) adds only that syscall when `CAP_SYS_ADMIN` is present. The host security configuration is unchanged.

Earlier attempts with the default Docker seccomp profile returned `bwrap: pivot_root: Operation not permitted`, including on native ARM. Those captures remain in `coding-tools-native/` and `coding-tools-namespaces/`. The corrected profile passed.

To repeat the local bridge check:

```sh
python3 experiments/dynamo-upstream/parity-run-1/readiness/check_transport.py \
  --output /tmp/parity-transport-check.json
```

This launches a temporary loopback server and a bridge-networked container. It makes no request to a model service. `transport-result.json` preserves the first failed check, which found a fixed-length response bookkeeping bug in the proxy; `transport-verified.json` records the corrected run.

## Launch the measured sessions

[launch-packet.json](launch-packet.json) contains the exact argument arrays and process environment for each client. Substitute workspace paths and pass each saved user-message file as one literal argument. Use [clients.md](../clients.md) for the effective model settings and [the protocol](../README.md) for request boundaries and cache resets.

`run_session.py` runs the three saved prompts, keeps one client session and capture proxy alive, and resumes the exact returned session ID. It admits up to seven calls in turn one, four more in turn two, and the remainder of the 15-call budget in turn three. After a phase reaches its cap it waits for all admitted HTTP responses to end, then interrupts the CLI with `SIGINT`. It records partial responses and interventions. A shared 1,500-second deadline covers all turns and their tools.

Use Claude on the Mac with the verified sandbox profile and a fresh study directory containing the frozen checkout at `task/`. The runner pre-authorizes its normal `Read`, `Edit`, `Write` and `Bash` tools and excludes personal credentials from the child environment. Its capture proxy forwards `127.0.0.1:18001` to the SSH tunnel at `127.0.0.1:18002`:

```sh
python3 "$PARITY_READY/run_session.py" \
  --client claude --binary /Users/mohamed/.local/bin/claude \
  --workspace /tmp/parity-claude-session \
  --upstream http://127.0.0.1:18002 --seconds 1500 --max-calls 15 \
  --trace-id-prefix parity-claude
```

Use Codex in the verified fresh container, with only the frozen task, study evidence, saved prompts, and pinned executable mounted. Use bridge networking for the tunnel connection. Mount the parity directory read-only at `/prompts` and prepare `/study/task`. Inside that container, start the relay and runner:

```sh
python3 /readiness/transport_relay.py --port 18000 --host-port 18002

python3 /readiness/run_session.py \
  --client codex --binary /codex --workspace /study \
  --prompt-dir /prompts --container-isolated \
  --upstream http://127.0.0.1:18000 --seconds 1500 --max-calls 15 \
  --trace-id-prefix parity-codex
```

The relay is a separate long-running process. The runner starts and stops its own proxy. It saves every turn's prompt, stdout, stderr, command, exit status and follow-up delay under `workspace/session`, alongside the HTTP captures. Its result records the same-session checks and request accounting. It verifies the executable hash against the launch packet before starting model traffic. Do not use an ephemeral Codex session.

Use a separate fresh workspace with `--smoke` for the one-request readiness check. It keeps the normal tool schema and uses a brief-answer prompt. The `--stub` mode exercises this controller entirely against local scripted responses. The saved [forced-cap Codex check](runner-cap/codex/result.json) interrupted after one completed response and resumed the same session twice. The normal [Claude](runner-verified/claude/result.json) and [Codex](runner-verified/codex/result.json) checks each completed three turns and four requests. Neither case makes a model request. A session missing either follow-up remains incomplete.

No GPU-serving result is claimed by this packet. Stock backend input-ID capture and request linkage, the deployment checks, cache reset, and both real smoke responses must pass before the measured sessions are accepted.

`launch_study.py` prepares the frozen public task and runs either client through that controller. It requires a new workspace. Without `--execute`, it only saves the task copy and exact command in `launch.json`:

```sh
python3 experiments/dynamo-upstream/parity-run-1/readiness/launch_study.py \
  --client claude --mode smoke --workspace /tmp/parity-claude-smoke \
  --binary /Users/mohamed/.local/bin/claude \
  --tunnel http://127.0.0.1:18002 --execute
```

For Codex, use `--client codex --binary /tmp/reedcode-parity-codex-linux-arm64/codex-aarch64-unknown-linux-musl` and another new workspace. Use `--mode full` for each measured session. Smoke checks allow one request and 180 seconds; full sessions allow 15 requests and 1,500 seconds. The helper verifies binary hashes; the controller checks the version before model traffic. Codex also verifies the local container image and disables image pulls. Logs and captures are saved under the workspace, with controller results in `session/`.

Clear the parity backend cache and router index before each invocation, save the reset evidence, and confirm the first measured request is cold. Keep smoke and full runs separate. The launcher only controls the local client; it neither resets the remote server nor changes the deployment. The existing `fresh_frontend.py` hook is specific to the prefill study and cannot reset parity unchanged.
