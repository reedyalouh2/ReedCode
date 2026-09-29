# Client configuration and capture

These are the configurations for [Parity Run 1](README.md). Both pinned CLIs completed streamed tool responses and two resumed follow-ups against the [CPU stub](readiness/README.md), then passed their live smoke tests and completed the [GPU sessions](../combined-gpu-20260929/PARITY.md). The study uses separate client configuration and session state.

## Claude Code 2.1.81

The documented gateway setup uses `ANTHROPIC_BASE_URL` and a gateway credential. The base URL is the capture proxy's origin, without `/v1`; the client adds `/v1/messages`. [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway-connect).

Use these process-local settings:

```text
CLAUDE_CONFIG_DIR=/tmp/reedcode-parity-client-state/claude
ANTHROPIC_BASE_URL=http://127.0.0.1:18001
ANTHROPIC_AUTH_TOKEN=local-dynamo
ANTHROPIC_MODEL=Qwen/Qwen3-8B
ANTHROPIC_DEFAULT_SONNET_MODEL=Qwen/Qwen3-8B
ANTHROPIC_DEFAULT_OPUS_MODEL=Qwen/Qwen3-8B
ANTHROPIC_DEFAULT_HAIKU_MODEL=Qwen/Qwen3-8B
MAX_THINKING_TOKENS=4096
CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

`local-dynamo` is a dummy credential for the isolated endpoint. Personal Anthropic keys and OAuth tokens must not enter the child environment. The model aliases keep ancillary inference on Qwen; those requests still count toward the call cap. Version 2.1.81 sent `thinking: {type: enabled, budget_tokens: 4096}`, `max_tokens: 8192` and 22 normal tools. The 4,096-token field is the client's requested thinking budget; it does not prove that the backend enforces a separate reasoning limit. [Environment settings](https://code.claude.com/docs/en/env-vars), [model settings](https://code.claude.com/docs/en/model-config).

Use the normal coding tools and system prompt. Do not use bare mode or remove the attribution preamble to improve prefix stability. Start one session in its clean task checkout and resume its exact session ID for the two saved follow-ups. Use `--print --verbose --output-format stream-json --include-partial-messages` when collecting noninteractive turns; the installed CLI documents `--resume` for continuation. Record any permission prompts and interventions. The capture controller enforces the shared request/time budget independently of CLI turn limits.

## Codex CLI 0.155.1

Use a named custom provider with the Responses wire protocol. The base URL includes `/v1`; Codex adds `/responses`. [Custom providers](https://learn.chatgpt.com/docs/config-file/config-advanced), [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

```toml
model = "Qwen/Qwen3-8B"
model_provider = "dynamo_parity"
model_context_window = 32768
model_reasoning_effort = "medium"
web_search = "disabled"

[model_providers.dynamo_parity]
name = "Dynamo parity"
base_url = "http://127.0.0.1:18001/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

Pass these entries as `-c` overrides with `--ignore-user-config --strict-config`, in a fresh container or other isolated account state. Version 0.155.1 rejects the obsolete `model_supports_reasoning_summaries` override. With the configuration above it sent `reasoning: {effort: medium, summary: auto}`, eight tool entries, and no `max_output_tokens` field. No proxy rewrites its output budget. Record the effective backend limit, output-limit stops and normal compaction policy.

Collect JSONL events with `codex exec --json --sandbox workspace-write --cd <clean-task-copy>`. Use `codex exec resume <exact-session-id>` for follow-ups, with the same provider overrides. Keep persistence for those three turns; an ephemeral session cannot supply the resume path. `--ignore-user-config` still consults normal authentication state, so it is insufficient by itself for isolation. The CPU check used a fresh Linux container with no personal mounts or external network, and confirmed that no authorization header reached the stub. That check does not establish GPU-serving compatibility.

## Stock capture path

Source was checked at the image's embedded revision. The [source manifest](source-check/manifest.json) records raw files and hashes; [source-check/README.md](source-check/README.md) records agreement with the release tag.

Start stock frontend with `--enable-anthropic-api`. Responses is enabled by default. Leave `--strip-anthropic-preamble` off. [Frontend flags](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/components/src/dynamo/frontend/frontend_args.py#L537), [Responses default](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/http/service/service_v2.rs#L676).

Proposed logging settings for readiness verification:

```text
DYN_REQUEST_TRACE=1
DYN_REQUEST_TRACE_SINKS=file
DYN_REQUEST_TRACE_FILE_FORMAT=jsonl
DYN_REQUEST_TRACE_RECORDS=request_end,request_payload
DYN_LOGGING_CONSOLE_FORMAT=jsonl
DYN_LOG=info,dynamo_llm::preprocessor=trace,dynamo_runtime::pipeline::network::ingress::push_handler=trace
```

Give frontend and backend different trace file paths. The preprocessor trace includes the full preprocessed request and its token IDs. The CPU check found that Python worker ingress logs only `PythonPayload(<PyAny>)`; that line cannot supply backend input IDs. The CPU capture worker recorded the payload in its callback, but this is custom test instrumentation and cannot establish a stock vLLM logging path. The [passive TCP check](wire-capture/README.md) verified byte decoding and explicit HTTP-to-wire linkage on the stock runtime. Keep its frontend log bridge and raw packets in the GPU evidence. [Preprocessor trace](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/preprocessor.rs#L6987), [opaque Python representation](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/bindings/python/rust/python_payload.rs#L33), [CPU evidence](../001-speculative-prefill/linux-readiness/stock-protocol-summary.json).

The HTTP recorder preserves the original bytes and streaming responses. It has no model fallback and adds no hints. Record semantic headers while omitting credentials. Capture KV events at the configured ZMQ endpoint; save process start identities independently because stock metrics alone do not establish restart continuity.

The final Anthropic `input_tokens` excludes cache reads. Normalize the final response before comparing clients: add uncached input, cache-read input and any separately reported cache-write input, then cross-check the backend's total prompt length. [Stock conversion](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/protocols/anthropic/types.rs#L525).

Thinking plus tool use becomes segmented reasoning in the stock Anthropic conversion, and the outgoing stream uses an `erased` thinking signature. Preserve that behavior and inspect the real client round trip. Its effect on reuse remains unmeasured. [Conversion](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/protocols/anthropic/types.rs#L351), [stream signature](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/protocols/anthropic/stream_converter.rs#L439).
