# Intended use and the first differing tokens

**Yes: both captured request shapes fit the documented use.** Tool continuations are explicitly described. The text control follows the general next-turn use case. The documentation provides no Qwen-specific guarantee, so the claim here concerns a documented feature exercised with a particular supported Chat request shape.

## What the official docs say

The [agent-hints guide](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/docs/fern/pages/use-cases/agents/agent-hints.md#L29-L55) describes warming the predicted next-turn prefix after the current turn and lists vLLM support. The [request-extension reference](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/docs/fern/pages/developer-guide/additional-resources/nvidia-request-extensions-nvext.md#L217-L235) describes appending the completed assistant response to the retained conversation and stripping thinking content for earlier turns.

The [agentic-inference article](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/docs/fern/pages/blog/2026/agentic-inference-optimizations.mdx#L77-L99) shows tools and `nvext.agent_hints.speculative_prefill` together, and describes warming before a tool returns. That directly supports testing an assistant tool call followed by its tool result. An ordinary user continuation follows the reference's broader multi-turn description.

These passages also appear in the pinned release, v1.5.0 at `b83b1d9304ebfc624709ac46db32b1b6f1ff1615`. Main was checked at `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`. The [source manifest](sources/manifest.json) records immutable URLs and full SHA-256 hashes for both revisions.

## What the recorded requests did

I rechecked the four received server payloads, their completed responses, and the server's prompt fingerprints. The [evidence file](intended-use-evidence.json) records each request ID, trace line, capture hash, and validation result.

| Check | Tool case | Text control |
| --- | --- | --- |
| Endpoint and activation | Streaming Chat; hint `true` | Streaming Chat; hint `true` |
| First response | One complete `record_value` call; `finish_reason=tool_calls` | Completed assistant text; `finish_reason=stop` |
| Next history | Original history, exact assistant tool call, matching tool result | Original history, exact assistant text, next user message |
| Preserved configuration | Same model, tool schema, and `tool_choice=auto` | Same model; no tools |

Both requests in each historical pair carried the hint. Each pair kept its session ID. There was no history rewrite or output-limit stop. The server used Qwen3-8B, thinking disabled, prefix caching, KV routing, and a 16-token block size. The logs confirm that the stock internal warmups executed; [provenance](../provenance.md) retains the attribution limits. The two-second wait allowed preparation to finish before the followup.

The reference places priority-scheduling backend notes after its speculative subsection. Our requests supplied no priority hint. The [speculative activation path](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/preprocessor/speculative_prefill.rs#L160-L169) checks the speculative flag alone; this deployment's successful dispatch records establish activation independently of that documentation ambiguity.

## Exact causes

- **Tool, token 40:** the messages-only [`SpeculativePrefillRequest`](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/preprocessor/speculative_prefill.rs#L105-L136) leaves `tools()` empty, so Qwen omits the system message's tool-definition section and emits token `13` (`.`) where the real followup emits `382` (`.\n\n`).
- **Text, token 56:** the [warmup builder](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/preprocessor/speculative_prefill.rs#L334-L360) renders the completed assistant as the final message, so Qwen inserts an empty thinking block beginning with token `151667` (`<think>`), while appending the real user turn moves that assistant into the template's earlier-turn branch and produces token `2307` (`ready`) instead.

Indices are zero-based. The pinned [Qwen template](https://huggingface.co/Qwen/Qwen3-8B/blob/b968826d9c46dd6066d109eabc6255188de91218/tokenizer_config.json) contains both branches. Its SHA-256 is `a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8`.

The tool accumulator also loses the newly generated assistant tool call. That is a separate loss after the earlier schema divergence; copying schemas alone leaves additional work. Regression tests should preserve that distinction and check the final token sequence after adding the next role.

Exact speculative token IDs are locally reconstructed. The archived runtime validates all complete prepared blocks, with eight trailing tool-case tokens outside its hashes. These checks establish the mismatch and documented use; current-main GPU behavior and performance remain unmeasured.
