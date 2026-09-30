# Preparing the right prefix

The local audit reproduces the mismatch from the [September 28 GPU run](../dynamo-20260928/README.md). It also identifies the smaller Qwen boundary problem: copying the missing fields alone still leaves an empty thinking block in the wrong place.

The candidate now preserves the completed assistant message and tool schema, renders the assistant in its next-turn position, and stops at a closed message boundary. Every prepared token matches the recorded continuation. A [follow-up GPU probe](gpu-20260928/README.md) confirmed the predicted reuse through an explicit preparation adapter. Dynamo's stock implementation remains unchanged.

The [full trajectory survey](trajectory-survey.md) checks all ten captured coding runs. Across 42 tool continuations, the candidate adds a median of 80 tokens beyond the preceding input's reusable blocks. The [boundary detail](boundary-detail.md) identifies the four tokens responsible.

## What changed in the tool case

| Rendered input | Tokens | Leading tokens shared with the follow-up | Full 16-token blocks shared |
| --- | ---: | ---: | ---: |
| Stock speculative request | 72 | 40 | 2 |
| Restore tool definitions | 204 | 198 | 12 |
| Also restore assistant tool calls | 223 | 198 | 12 |
| Render the next role and stop at a closed boundary | 208 | 208 | 13 |

The last row has been rounded down from a 218-token closed prefix. The original request already shares 198 tokens with the next request, or 12 full blocks. Its generated suffix cannot repair that earlier mismatch. This example therefore has room for **one extra full block**, even after the prefix is corrected. The text example gains zero full blocks.

[results.json](results.json) records both cases. [token-ids.json](token-ids.json) exports their initial prompts, candidates, and actual follow-ups for a separate cache probe.

## Why the boundary matters

The pinned Qwen template inserts `<think>\n\n</think>\n\n` when an assistant is the last message, even with thinking disabled. Once a tool result follows, an assistant without reasoning text is rendered without that empty block. The two prompts diverge before the tool call. A later ordinary user turn also changes which earlier reasoning the template retains.

The candidate builder appends a placeholder for the expected next role. It removes that placeholder and the future header, stopping at the assistant's closing `<|im_end|>` token. It then keeps complete cache blocks. The placeholder has no real tool content. The actual follow-up is used only afterward to audit the candidate.

This construction is specific to the pinned Qwen template. It assumes unchanged history, tools, and template settings. A context revision invalidates it. Tool continuations keep their expected call IDs; text continuations assume an ordinary user message. The audit rejects unsupported formats and the Qwen special case where a user message is wrapped in `<tool_response>` tags.

## Run locally

Only the tokenizer is downloaded. No model weights or GPU are needed.

```bash
mkdir -p /tmp/reedcode-prefix-tokenizer
curl -fL \
  https://huggingface.co/Qwen/Qwen3-8B/resolve/b968826d9c46dd6066d109eabc6255188de91218/tokenizer.json \
  -o /tmp/reedcode-prefix-tokenizer/tokenizer.json

uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/audit.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --output /tmp/prefix-results.json \
  --export /tmp/prefix-token-ids.json

REEDCODE_QWEN_TOKENIZER=/tmp/reedcode-prefix-tokenizer/tokenizer.json \
  uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python -m unittest discover -s tests -p 'test_dynamo_prefix*.py'
```

The script checks the tokenizer and template hashes. The tests cover the recorded mismatch, changing tool output, reasoning text, parallel tool calls, context revisions, partial responses, and block rounding. The tokenizer-dependent tests skip if the downloaded file is absent.

## Check actual cache reuse

With the pinned model served behind Dynamo and prefix caching enabled:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/probe_gpu.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --base-url http://127.0.0.1:18000/v1 \
  --server-identity 'path or identifier of the deployment record' \
  --output /tmp/prefix-gpu-results.json
```

The probe makes ten completion requests, each with a one-token output budget. Each condition gets a fresh identifier at the start of the system message. It checks that the first request has zero cached tokens and that the server sees the submitted token count. It then sends the original prompt, optionally prepares the candidate, and sends the fixed follow-up. The expected hit count is computed separately for each identifier, since token lengths vary.

The probe sends prepared token IDs directly through `/v1/completions`. It records every request, response, and usage count, including the preparation request. It tests this explicit preparation adapter. It does not install a server-side fix or exercise a full agent trajectory. A single generated token is sufficient for the initial request here because the continuation already diverges within the input prompt; more sampled output cannot extend that shared prefix.

The command refuses to overwrite an existing record and exits with a failure status when cache counts differ from the expected values. `--server-identity` is an operator-supplied reference; the probe does not independently verify the deployed image or model revision.

Add `--cases tool text tool_long` to include a longer synthetic argument. This copies the tool capture and appends 512 copies of the word `context` to the completed assistant's `value` argument. The initial prompt stays the same, apart from each condition's fresh identifier. The matching assistant history in the follow-up gets the same edit. This adds five requests to the probe. The model did not generate the longer argument, and the tool is never executed; the case checks whether reuse extends across more cache blocks.

## Limits

Jinja2 renders the real pinned template with Dynamo's JSON spacing and key order. All 314 saved server inputs match its token fingerprints, including final partial blocks. The separate GPU probe verifies reuse of submitted token IDs. The corrected preparation policy's effect on workflow latency and quality remains unmeasured.

The two small examples are regression cases. They provide little headroom for a scheduling policy. The next experiment needs longer useful continuations or cache pressure, and must account for preparation costs and ordinary decode reuse.

[upstream/README.md](upstream/README.md) contains the source finding and the remaining server work.

## New-user boundary probe

This CPU-only probe uses four authored user turns, synthetic reasoning, and diagnostic tool output. It isolates template behavior without model requests:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/user_boundary.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --output /tmp/user-boundary-results.json
```

[user-boundary-results.json](user-boundary-results.json) records all 18 boundary comparisons. The longer suffix losses demonstrate a mechanism. They provide no estimate of its frequency, GPU cost, or task quality. The fixture rejects changed history and schemas; it cannot classify real compaction. ReedCode currently has no compaction path.

The [family source record](family-template-evidence.json) pins two additional models and 14 constructed rendering checks. To reproduce them, download its listed artifacts into `<directory>/<family>/<file>`. Extract DeepSeek's `chat_template.jinja` from the `chat_template` string in its pinned `tokenizer_config.json`. Then run:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/family_templates.py \
  --artifacts /tmp/reedcode-family-template-audit
```

The script verifies every artifact hash before comparing the rendered outputs. It loads only tokenizer data and templates.
