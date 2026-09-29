# Local speculative-prefill fix

The [local patch](fix/README.md) implements a narrow Qwen3 tool-continuation path. It preserves the request and completed assistant, then asks the renderer for a supported continuation prefix. Unknown contracts skip preparation. The [split plan](upstream-split.md) separates the general request-preservation change from the renderer hook.

## Pinned source

- Dynamo `main`: `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`, with renderer 5.4.0.
- The unchanged [main speculative-prefill source](https://github.com/ai-dynamo/dynamo/blob/f5d3353e2167bb0f0d729085eb5bc9183bf4b222/lib/llm/src/preprocessor/speculative_prefill.rs) has SHA-256 `d1ea24631f4d7c9e731615948bc9a128afdd70a4326bad87cf1569200b1535fb`.
- The [1.5.0 source corresponding to the deployment](https://github.com/ai-dynamo/dynamo/blob/b83b1d9304ebfc624709ac46db32b1b6f1ff1615/lib/llm/src/preprocessor/speculative_prefill.rs) has SHA-256 `a8f4bebc8c3db3c7022f8a2a29c1d6f7499b7f944df1e6a91c7f033a3a12d794`.
- Supported model artifacts: Qwen/Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`. The patch checks the tokenizer/config checksums and the selected template's exact source.

The renderer is published separately in [frontend-crates](https://github.com/ai-dynamo/frontend-crates). Review therefore needs both the renderer capability and Dynamo's use of it. A local Cargo override connects the patches for CPU validation; a deployable build needs pinned artifacts for both.

## What changed

The old request wrapper carried messages alone. The patch clones the effective request after thinking defaults and normalization have been applied. The continuation wrapper delegates tools, tool choice, response format, reasoning settings and template arguments. It changes only the messages and generation-prompt flag.

The stream accumulator preserves content, reasoning, tool-call IDs, names and raw argument fragments. It waits for EOF after one completed `tool_calls` choice. Length stops, refusals, errors, malformed arguments, conflicting metadata and multiple choices skip preparation. Every client chunk is forwarded unchanged.

The renderer capability defaults to unsupported. For the exact Qwen template, it appends a placeholder tool result so the completed assistant is rendered in its future history position. It then cuts the prompt at the assistant's closing `<|im_end|>`. The placeholder never reaches the backend. This template's earlier history does not depend on the content of a tool-role message, which makes that boundary usable for arbitrary tool-result text under the same request settings.

Tokenization requires the pinned artifacts, the default Hugging Face backend, a compatible prefix-cache contract, zero added postprocessor tokens and the expected special-token boundary. A template match alone would not establish a token-prefix match.

The warmup uses the actual model name and retains the one-token generation bound. Main's cancellation, timeout, admission and downstream-drain controls remain in place.

## Support boundary

This version supports completed Qwen3 tool turns with unchanged message history, tools and effective rendering settings. Thinking on and off are covered. It skips:

- Text continuations and other or modified templates.
- Unrecognized template arguments, multimodal history, legacy function calls and continue-final-message requests.
- Incomplete, malformed, refused, limited or multi-choice responses.
- Changed tokenizer artifacts, unsupported tokenizer backends or an incompatible closing-token contract.
- LoRA, cache namespaces and nondefault routing hints, including OSL, priority and explicit worker targeting.
- Explicit token input, raw-prompt requests, NUL-containing prefixes and the optional extra tool-argument normalization path.

This reduces the feature's coverage. Extending it requires another explicit rendering and tokenizer contract with regression evidence. The [scope audit](scope/README.md) shows why a general final-assistant render cannot supply that guarantee: Qwen, Nemotron and GPT-OSS have different tool and text boundaries.

## Tests and validation

The native CPU renderer produces exact follow-up prefixes in **54/54 cases**: 42 captured tool continuations and 12 constructed length controls. Ordinary request tokens are unchanged. Preparation receives the captured assistant message; future tool results are withheld.

Passing helper regressions cover fragmented and parallel tool calls, reasoning, raw argument preservation, malformed/incomplete responses, changed artifacts, unsupported templates and settings, multi-turn histories, and tool text containing template delimiters. The [fix packet](fix/README.md) records the commands, logs and hashes.

The patched `dynamo-llm` crate compiles with test targets on macOS ARM64. All 19 warmup-module tests pass, including the 13 adapted upstream lifecycle tests and six accumulator tests. These use a mock backend. Matched Linux frontends then passed the live generated-tool checks and the 18-trial GPU comparison.

## GPU validation

The [GPU comparison](../combined-gpu-20260929/PREFILL.md) measured first and repeated warmups, scheduled prefill, follow-up hits and the retained branch. Stock added 62.27% and 85.20% prefill work versus off, with no extra real-request hits. The fix cut prefill 37.96% and 45.96% versus stock and removed the warmup-only branch.

The complete tested fix supplies those results. The smaller request-preservation contribution will need its own tests after extraction; the cross-model ablation already shows why it is useful. [Proposed split](upstream-split.md).
