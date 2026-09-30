# Client configuration and capture

Both pinned CLIs completed the [GPU sessions](../combined-gpu-20260929/PARITY.md) with separate configuration and session state. These settings record their custom endpoints.

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

The GPU sessions used all three recommended frontend flags:

```text
--enable-anthropic-api
--strip-anthropic-preamble
--enable-streaming-tool-dispatch
```

The worker used `--dyn-tool-call-parser hermes`, `--dyn-reasoning-parser qwen3` and `--dyn-default-thinking-mode enabled`. The [study manifest](../combined-gpu-20260929/manifest.json) pins the image, model, actual commands and configuration captures.

The HTTP recorder kept request bodies and streaming responses, with credentials excluded. Passive TCP captures supplied the token IDs at the backend boundary. `join_wire.py` joins them to HTTP requests, and KV events account for inserted cache blocks. Process identity was checked separately because the metrics endpoint omitted a restart-detection counter.

Anthropic `input_tokens` excludes cache reads. The report adds uncached input, cache-read input and any cache-write input, then checks that total against the backend prompt length. [Stock conversion](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/protocols/anthropic/types.rs#L525).

The [parity report](../combined-gpu-20260929/PARITY.md) records the reasoning and tool-call round trips, within-turn reuse and cross-turn changes.
