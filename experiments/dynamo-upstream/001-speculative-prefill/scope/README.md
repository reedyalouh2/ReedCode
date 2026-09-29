# Which templates are affected?

I ran 16 authored cases against each pinned Dynamo revision: release `b83b1d9304ebfc624709ac46db32b1b6f1ff1615` and main `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`. Both produced identical rendered outputs. The missing schema affects Qwen, Nemotron and GPT-OSS in these cases. The next-turn boundary also matters, but its behavior depends on the template and the assistant message.

These use the same compiled upstream warmup construction as the [current-source check](../current-code/README.md), with each model's official pinned tokenizer and template. No model generated the messages. [model-pins.json](model-pins.json) records every source and artifact hash; [results.json](results.json) retains all inputs' rendered strings, token IDs, errors and comparisons.

## Tool continuations

The baseline case has one tool definition, an assistant tool call with empty text, and its tool result. Each number below is the count of initial tokens shared with the real follow-up prompt. Thinking is enabled for Qwen and Nemotron; disabling it gives the same counts here.

| Template | Stock | Schema restored | Schema, settings and full assistant restored | Complete prefix after restoration? |
| --- | ---: | ---: | ---: | --- |
| Qwen3-8B | 7 | 157 | 157 | No; 182 tokens prepared |
| Nemotron-3-Nano | 7 | 280 | 306 | Yes; 306 tokens prepared |
| GPT-OSS-20B | 59 | 133 | 150 | Yes; 150 tokens prepared |
| DeepSeek-R1 | Unavailable | Unavailable | Unavailable | Follow-up rendering error |

Restoring the schema changes only `tools()`. It puts the tool-definition marker back into the first three templates' prefixes. The next step restores template settings; that changes no preparation in this matrix. The final step restores the complete assistant message, including tool calls and any separate reasoning field.

The R1 template ignores the active tool schema in these fixtures. Its tool-follow-up render fails when argument strings become maps, so it supplies no prefix comparison. That error occurs with both pinned renderer versions and stays visible in the report.

Qwen needs another distinction. With an empty reasoning field, its final assistant gets an empty `<think>` block during preparation. The same assistant before a tool result loses that block. When the assistant has separate nonempty reasoning and is fully restored, the preparation becomes an exact 187-token prefix. Preserving the message fixes that case.

## The text boundary

The archived Qwen text case diverged at token 56. This shorter fixture diverges at token 23 for the same empty-think-block reason. The position follows the prompt; it is not a model-wide constant.

| Template | Plain assistant, followed by a new user | Separate reasoning control |
| --- | --- | --- |
| Qwen3-8B | Mismatch with thinking on and off | Mismatch remains after restoring the full assistant |
| Nemotron-3-Nano | Exact 30-token prefix with thinking on and off | Mismatch: the next user changes how past reasoning and its surrounding whitespace render |
| DeepSeek-R1 | Exact 16-token prefix | No separate reasoning control in this matrix |
| GPT-OSS-20B | First 89 tokens match; final token differs | No separate reasoning control in this matrix |

GPT-OSS writes `<|return|>` for a final assistant rendered without a generation prompt. The same assistant in conversation history ends with `<|end|>`. Nemotron's passing plain-text case therefore does not imply that all its histories preserve prefixes.

## Reproduce

Qwen and Nemotron have explicit `enable_thinking=true/false` cases. Their nonempty-reasoning controls use the enabled setting. R1 and GPT-OSS use their native defaults; their templates offer no equivalent on/off switch. GPT-OSS's template also inserts the current UTC date, which is recorded by this run and can change future output bytes.

The run used Rust 1.96.1. Every one of the 299 resolved registry packages in each build matches that revision's upstream lockfile. Generated source, lockfiles, build logs and raw outputs are under [compiled](compiled). The binaries and downloaded tokenizers stayed under `/tmp`.

From the repository root, with the pinned tokenizers at the paths below:

```bash
RUSTUP_HOME=/tmp/reedcode-rustup CARGO_HOME=/tmp/reedcode-renderer-cargo uv run --with-requirements experiments/dynamo-prefix/requirements.txt python experiments/dynamo-upstream/001-speculative-prefill/scope/reproduce.py --qwen-tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json --artifacts /tmp/reedcode-family-template-audit --cargo /tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/cargo --rustc /tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/rustc --build-root /tmp/reedcode-stock-scope --output /tmp/reedcode-stock-scope-rerun.json --offline
```

Choose a new output path. The script checks model and upstream-source hashes before rendering. It records both byte-prefix and token-prefix comparisons; all reported mismatches here also change the rendered text.

## Limits

This is a small diagnostic matrix. It exercises the official templates through the pinned standalone Rust formatter and tokenizer. Full frontend model selection, other request transforms, stream assembly, backend cache behavior and latency remain untested. A mismatched preparation can still share useful earlier tokens. These prefix counts establish serialization behavior; cache reuse requires a separate backend measurement.
