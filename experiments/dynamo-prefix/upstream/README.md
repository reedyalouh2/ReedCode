# Source finding

The September 28 deployment used Dynamo 1.5.0 at image commit `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`. Its [speculative-prefill source](https://github.com/ai-dynamo/dynamo/blob/32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653/lib/llm/src/preprocessor/speculative_prefill.rs) is preserved in [speculative_prefill-1.5.0.rs](speculative_prefill-1.5.0.rs).

`SpeculativePrefillRequest` carries only messages. Its `OAIChatLikeRequest` implementation omits tools and template arguments. The stream wrapper accumulates assistant text and discards tool-call deltas. `prefill_task` appends that text as the final assistant message and renders it with `add_generation_prompt=false`.

There are three distinct problems for the recorded Qwen tool continuation:

1. Dropping tool definitions changes the system prefix.
2. Dropping the assistant's tool calls changes the completed conversation.
3. Rendering the assistant as the last message retains an empty thinking block that disappears when the next tool message arrives.

The [local audit](../README.md) reproduces these differences with the pinned tokenizer and template. The captured tool example shares 40 tokens with the stock preparation, 198 after restoring the missing fields, and all 208 tokens of the block-rounded candidate after accounting for the future role.

I also inspected [main at `b75173c448d792615e886a5dc17627ca5f4a57be`](https://github.com/ai-dynamo/dynamo/blob/b75173c448d792615e886a5dc17627ca5f4a57be/lib/llm/src/preprocessor/speculative_prefill.rs). It adds task lifetime and admission controls. The messages-only speculative request and text-only accumulator remain. This is a source inspection; the newer server has not been run here.

## Server changes to implement and test

Preserve the original rendering inputs and assemble a complete assistant message from streamed deltas, including reasoning and tool calls. Skip incomplete or ambiguous responses. Resolve the expected continuation role and use a renderer capability that produces a stable prefix for that model's template. Unsupported templates should skip preparation.

The server must retain its existing cancellation, admission, and request-lifecycle behavior. A follow-up context revision must invalidate any scheduled preparation. Keep the engine's normal model, tokenizer, adapter, tenant, and cache-key checks.

The small Python candidate builder is an executable reproduction and an input to the GPU cache probe. It is not a production Rust patch. Before proposing a patch upstream, run renderer regression tests for supported templates and the prepared-prefix cache probe against the rebuilt server.

Before attributing a future speedup to scheduling, compare against a template whose serialization stays stable as the conversation grows. The current Qwen boundary change makes the previous decode's assistant tokens unusable in the next prompt. Longer tool arguments could amplify that cost. A template change might remove some need for preparation, but its effects on model behavior and task quality need their own checks.

## Source hashes

| File | SHA-256 |
| --- | --- |
| Deployment `speculative_prefill.rs` | `a8f4bebc8c3db3c7022f8a2a29c1d6f7499b7f944df1e6a91c7f033a3a12d794` |
| Inspected main `speculative_prefill.rs` | `d1ea24631f4d7c9e731615948bc9a128afdd70a4326bad87cf1569200b1535fb` |

The copied source retains NVIDIA's Apache-2.0 header and [license](LICENSE).
