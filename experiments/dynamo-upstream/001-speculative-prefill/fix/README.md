# Local speculative-prefill fix

The patched renderer produces an exact prefix for all **42 recorded tool continuations and 12 constructed length controls**. The ordinary request and follow-up tokens are unchanged. The future tool result is withheld during preparation. [Raw tokens, hashes and results](results.json).

The original isolated tool pilot now prepares an exact 218-token prefix of its 244-token follow-up. Its text-only control skips warmup. Both normal token sequences are unchanged. [Minimal-pilot output](pilot-native-output.json).

This is a local patch for the pinned Qwen3 tool-continuation path. The [GPU comparison](../../combined-gpu-20260929/PREFILL.md) completed 18 trials and confirmed that it removes the separate warmup branch. It has not been submitted upstream.

## What changed

The old builder loses tool definitions, template settings and the completed assistant tool call. It also renders the final assistant differently from the historical assistant that a tool result follows.

The fix keeps the effective request and collects the complete streamed assistant, including reasoning and parallel tool calls. It waits for EOF, checks that the turn finished successfully with tool calls, and preserves the original argument strings. Partial responses, errors and ambiguous choices skip preparation. Client response frames pass through unchanged.

The renderer exposes an optional tool-continuation prefix method. For the verified Qwen template it renders a placeholder tool result, then keeps only the preceding closed assistant message. Ending at `<|im_end|>` avoids a token merge with unknown future content. The existing admission, cancellation, timeout and cleanup machinery remains in place.

## Support boundary

The implementation checks the actual template source, tokenizer/configuration checksums and tokenizer behavior. A matching model name is insufficient. It supports the pinned Qwen3-8B artifacts, the default Hugging Face tokenizer, text history, default routing, and the tested thinking settings.

Unsupported templates and text-only completions skip warmup. The same applies to custom template arguments, multimodal input, partial messages, multiple choices, explicit token input, raw-prompt mode, argument normalization, LoRA/cache namespaces and nondefault routing hints. Literal NULs in the prepared prefix also skip: normal Dynamo preprocessing removes them. Clients must preserve the completed assistant and effective rendering fields when appending tool results.

This deliberately limits the first patch. The other model families in the [scope audit](../scope/README.md) need their own verified continuation contracts. Skipped requests still receive normal responses.

## CPU checks

| Check | Result |
| --- | --- |
| Accumulator, token-boundary and request-contract regressions | 14 passed |
| Full patched renderer library suite | 194 passed, 2 ignored |
| Recorded tool continuations | 42/42 exact prefixes |
| Constructed length controls | 12/12 exact prefixes |
| Ordinary input and follow-up tokens | Unchanged in all 54 cases |
| Full `dynamo-llm` test-target compilation | Passed on macOS ARM64 |
| Dynamo async lifecycle and accumulator tests | 19 passed |
| ReedCode repository suite, saved CPU check | 215 passed, 1 skipped |

The regressions cover reasoning on/off, multi-turn history, parallel tools, fragmented streams, malformed arguments, late errors, unsupported templates/settings, marker collisions, NUL normalization and tokenizer changes. All 13 existing lifecycle tests and their 81 assertions are retained with tool-call fixtures. Their formatter/tokenizer doubles isolate lifecycle behavior; the native renderer/tokenizer tests cover token parity separately.

Saved logs: [native tests](cpu-tests.log), [renderer suite](renderer-tests.log), [Dynamo compilation](integration-build.log), [Dynamo tests](integration-tests.log), [repository tests](repository-tests.log). Exact commands and source/log hashes are in [integration-results.json](integration-results.json). The [integration runner](integration_check.py) checks the source revision and patched files before compiling and running the actual module in Dynamo.

## Pins and patch layout

- Dynamo main: `f5d3353e2167bb0f0d729085eb5bc9183bf4b222`.
- `dynamo-renderer` 5.4.0, `dynamo-protocols` 6.1.0, `dynamo-tokenizers` 1.9.1.
- Renderer crate source provenance: frontend-crates commit `8cd55a137ab28e69479a6e5777f9fddc4eb0eb30`, directory `renderer`.
- Qwen3-8B revision: `b968826d9c46dd6066d109eabc6255188de91218`.
- Rust 1.96.1; default macOS ARM64 target for these CPU checks.

[dynamo.patch](dynamo.patch) applies at the pinned Dynamo repository root. [renderer.patch](renderer.patch) applies at the renderer crate root; in the frontend-crates repository, apply it with `git apply --directory=renderer`. The renderer trait change and Dynamo caller must be tested together. The GPU frontend needs both patches.

The [build script](build.py) verifies the saved Dynamo sources and the published renderer archive against the upstream lockfile before patching. It uses the upstream dependency identities; the native check has 298 registry dependencies and no identity drift. Generated sources and lockfiles are saved here. Local binaries remain under `/tmp`.

## Reproduce

From the ReedCode root, with the [stock CPU reproduction prerequisites](../current-code/README.md#reproduce) installed:

```bash
uv run python experiments/dynamo-upstream/001-speculative-prefill/fix/build.py
uv run python experiments/dynamo-upstream/001-speculative-prefill/fix/reproduce.py \
  --binary /tmp/reedcode-current-stock-prefill/target/debug/reedcode-prefill-fix-check \
  --output /tmp/reedcode-prefill-fix-results
```

The scripts accept compiler, build-root and tokenizer paths. The saved manifests use this machine's isolated `/tmp` Cargo cache. They require cached public crates; no model weights or GPU are used.

For the full Dynamo check, apply `dynamo.patch` in a separate checkout of the pinned revision and build the patched renderer above. Run from ReedCode with the recorded local toolchain:

```bash
PROTOC=/tmp/reedcode-protoc-29.5/bin/protoc \
SWAGGER_UI_DOWNLOAD_URL=file:///tmp/reedcode-swagger-ui-v5.17.14.zip \
RUSTUP_HOME=/tmp/reedcode-rustup \
CARGO_HOME=/tmp/reedcode-renderer-cargo \
CARGO_TARGET_DIR=/tmp/reedcode-dynamo-integration-target \
CARGO_BUILD_JOBS=2 \
RUSTC=/tmp/reedcode-rustup/toolchains/1.96.1-aarch64-apple-darwin/bin/rustc \
uv run python experiments/dynamo-upstream/001-speculative-prefill/fix/integration_check.py \
  --checkout /tmp/reedcode-dynamo-prefill-fix \
  --output /tmp/reedcode-prefill-integration-rerun
```

The runner invokes Cargo from the Dynamo checkout so its `tokio_unstable` build setting is loaded. The first build also requires Protobuf and the public Swagger UI 5.17.14 assets; their download URLs and hashes are in [build-dependencies.json](build-dependencies.json). The macOS check uses Dynamo's no-default-features build and NIXL's CPU stub fallback. It cannot validate CUDA kernels or the Linux serving artifact.

## GPU result and review

The matched Linux frontends completed generated-tool smoke tests and 18 off/stock/fixed replay trials. The fix cut scheduled prefill by 37.96% and 45.96% versus stock in the two sessions. Real follow-ups gained 368 and 64 cached tokens, and the warmup-only branch disappeared. [Counts, timings, retained blocks and raw evidence](../../combined-gpu-20260929/PREFILL.md).

These controlled replays leave concurrent serving and agent quality unmeasured. A fresh upstream issue/PR search and review of both patches precede filing.
