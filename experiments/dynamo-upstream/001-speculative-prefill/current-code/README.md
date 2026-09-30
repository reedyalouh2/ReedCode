# Current-source CPU reproduction

I compiled the stock warmup request and assistant-construction code from the latest stable release and a fresh main revision. Both produce the same mismatched prefixes as the archived run.

| Source | Renderer / protocols / Dynamo tokenizer | Text: first different token | Tool: first different token |
| --- | --- | ---: | ---: |
| v1.5.0, `b83b1d9304ebfc624709ac46db32b1b6f1ff1615` | 5.1.0 / 5.4.0 / 1.8.0 | 56 | 40 |
| main, `f5d3353e2167bb0f0d729085eb5bc9183bf4b222` | 5.4.0 / 6.1.0 / 1.9.1 | 56 | 40 |

Token indices start at zero. The text preparation has 64 tokens; its follow-up has 78. The tool preparation has 72 tokens; its follow-up has 244. Neither preparation is a prefix of its follow-up. Full token IDs and rendered strings are in [results.json](results.json).

The [source snapshot](sources/manifest.json) was fetched on September 28, 2026. The latest stable release was v1.5.0. Main uses renderer 5.4.0 at this revision; the earlier 5.1.2 build attempt is superseded for this check.

## What ran

[reproduce.py](reproduce.py) verifies the source hashes, then copies three unchanged pieces into a small Rust crate: `SpeculativePrefillRequest` and its implementations, the assistant-message construction, and the formatter call. The report records each source line range and fragment hash. The remaining [runner](runner.rs) loads inputs, calls the actual published Rust formatter, and tokenizes its output with that revision's `dynamo-tokenizers::HuggingFaceTokenizer`.

The stock request gets its actual trait defaults. It has no tools, no template arguments, and `add_generation_prompt=false`. The normal-request adapter supplies `enable_thinking=false` to represent the archived deployment's thinking setting. Both its original and follow-up prompts match every archived input-sequence hash, including partial blocks, under both dependency sets.

The prepared inputs also match the archived routing lengths and all four full-block hashes for each case. The tool preparation has eight trailing tokens that the runtime did not hash. Their exact IDs come from this compiled reproduction.

Each build resolved 299 registry packages. Every package's name, version, source and checksum matches its revision's upstream lockfile. The runner rebuilds each revision from the pinned sources. [results.json](results.json) records source, dependency and binary hashes; [compiled/main](compiled/main) retains the resolved main lockfile and output used by the fix checks.

An offline rerun reproduced the input, output, generated source and lockfile bytes. Debug binary hashes changed across rebuilds; the report identifies the final recorded binaries.

## Reproduce

The recorded build used Rust 1.96.1 installed under `/tmp`, leaving the existing compiler and shell configuration alone. It requires the pinned Qwen tokenizer already used by the prefix audit. From the repository root:

```bash
RUSTUP_HOME=/tmp/reedcode-rustup CARGO_HOME=/tmp/reedcode-renderer-cargo uv run --with-requirements experiments/dynamo-prefix/requirements.txt python experiments/dynamo-upstream/001-speculative-prefill/current-code/reproduce.py --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json --cargo /tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/cargo --rustc /tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/rustc --build-root /tmp/reedcode-current-stock-prefill --output /tmp/reedcode-current-stock-rerun.json
```

Use a new output path. On another machine, point `--cargo` and `--rustc` at its Rust 1.96.1 installation. `--offline` works once the pinned crates are cached. No model weights or GPU are needed.

## Scope

This compiles the exact request construction and rendering path in isolation. Source checks also confirm that the preprocessor invokes the wrapper and that the stream accumulator reads text deltas and triggers on `finish_reason`. The stream itself is not executed here: response text is taken from the two captured completed single-choice responses.

The full Dynamo frontend, async admission and cancellation, backend dispatch, cache residency and latency are outside this check. Runtime hash evidence comes from the archived 1.5.0 deployment. Matching those hashes on main establishes this CPU path's token behavior; it supplies no new main-branch server or GPU measurement.
