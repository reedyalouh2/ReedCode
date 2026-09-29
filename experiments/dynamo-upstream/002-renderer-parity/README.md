# Standalone Rust formatter check

I compiled the published `dynamo-renderer` 5.1.0 crate on the Mac and compared it with the official templates on 44 constructed cases. This runs the Rust formatter used by Dynamo. It does not launch the full frontend or an inference engine.

| Family | Exact rendered-text matches | Rust rendering errors |
| --- | ---: | ---: |
| Qwen3-8B | 16/16 | 0 |
| DeepSeek-R1 | 4/8 | 4 |
| Nemotron-3-Nano | 20/20 | 0 |

The four DeepSeek tool cases fail because the formatter converts historical JSON argument strings into maps, while the pinned R1 template uses string concatenation for those arguments. The ordinary assistant cases match. [results.json](results.json) retains the errors and every successful rendered string.

Argument-format regressions are already discussed upstream. The [existing-work check](known-issues.md) records that overlap and the newer renderer release. This result establishes the pinned 5.1.0 standalone failure; current-runtime behavior is unverified.

A separate [5.1.2 build attempt](renderer-5.1.2-build-attempt/build-attempt.json) stopped at a tokenizer dependency's compiler requirement on Rust 1.90.0. It produced no rendering result.

Qwen and Nemotron cover `enable_thinking=true` and `false`. DeepSeek-R1's pinned template has no such switch, so its cases omit it. Tool cases run with and without active tool schemas. The settings are recorded in each result.

Nemotron needs the opposite treatment: its template expects argument maps. The Python reference receives a map; the Rust formatter receives the equivalent OpenAI JSON string and normalizes it. That conversion is recorded per case. It explains why direct Jinja behavior alone cannot establish frontend behavior.

## What is pinned

`Cargo.toml` pins renderer 5.1.0, protocols 5.4.0, Dynamo tokenizers 1.8.0, MiniJinja 2.24.0, and serde_json 1.0.150 to the versions in Dynamo 1.5.0's lockfile. MiniJinja's `preserve_order` feature matches the frontend's configuration. `Cargo.lock` records the remaining resolved dependencies.

The wrapper implements `OAIChatLikeRequest`, delegates ordinary fields to the protocol request, and forwards explicit template arguments. `PromptFormatter::from_parts` performs the actual Rust rendering. Both output strings are then tokenized with the same pinned Python tokenizer so token differences can be inspected. Rust tokenization is outside this check.

The configs and tokenizers are checked against the [family artifact pins](../../dynamo-prefix/family-template-evidence.json) and the existing Qwen audit. Model weights are unnecessary. Cargo caches and compiled binaries stayed under `/tmp` during this run.

## Reproduce

From the repository root, build the small crate:

```bash
CARGO_HOME=/tmp/reedcode-renderer-cargo CARGO_TARGET_DIR=/tmp/reedcode-renderer-target cargo build --locked --manifest-path experiments/dynamo-upstream/002-renderer-parity/Cargo.toml
```

Download the pinned family artifacts listed in the linked manifest into `deepseek-r1/` and `nemotron-3-nano/` under an artifact directory. Use the pinned Qwen tokenizer from the prefix audit. Then run:

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt python experiments/dynamo-upstream/002-renderer-parity/compare.py --binary /tmp/reedcode-renderer-target/debug/reedcode-renderer-parity --artifacts /tmp/reedcode-family-template-audit --qwen-tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json --output /tmp/reedcode-renderer-parity.json
```

The output path must be new. The report records source, config, tokenizer, lockfile, and binary hashes.

## Limits

These are authored messages, including reasoning and tool results. No model generated them. The Python reference runs the shipped templates with explicitly recorded argument representations. The Rust side additionally applies its own message normalization.

This check does not cover native formatter selection, model-default merging, extra preprocessor transforms, multimodal inputs, or engine-specific rendering. A full-server request may take additional paths. The DeepSeek error is established for the pinned standalone formatter path. No GPU cache behavior, latency, or model quality was measured.
