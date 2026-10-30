# Thinking disabled, but still running

Dynamo's Anthropic request converter dropped `thinking: {"type":"disabled"}`. With Qwen3's thinking-enabled default, the model kept reasoning. My patch carries the explicit false value into the chat template.

The fix merged into Dynamo on October 16 in [PR #16608](https://github.com/ai-dynamo/dynamo/pull/16608), closing [issue #16600](https://github.com/ai-dynamo/dynamo/issues/16600). The patch and GPU evidence below preserve the original study revision; the merged PR includes the updated converter and HTTP regression tests.

I tested eight fixed inputs with three paired repeats on an A100. Stock generated reasoning in all 24 disabled requests; the fix generated none. All 48 calls passed token-accounting and cache-isolation checks.

## Results

Each input set has 12 calls per condition. Response time is the sum of complete request durations, including output-limit stops.

| Input set | Stock output tokens | Fixed output tokens | Reduction | Stock response time | Fixed response time | Reduction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Four Claude Code snapshots | 12,744 | 3,291 | 74.2% | 334.91 s | 116.19 s | 65.3% |
| Four short Python questions | 16,011 | 141 | 99.1% | 365.28 s | 3.69 s | 99.0% |

Stock generated 23,043 reasoning tokens across the two sets. Six stock responses reached the 2,048-token output limit. The fixed condition had no reasoning tokens or limit hits. Token counts repeated exactly across each input's three runs.

**Short-question accuracy fell from 3/4 to 2/4**, repeated as 9/12 and 6/12. The generator question became wrong with thinking off. The regex question failed in both conditions; the other two stayed correct. The fix restores the caller's choice, with the expected possibility of different answers. Total token savings include different final answers and tool actions.

The Claude snapshots came from four fixed positions in one earlier session. I changed their original enabled-thinking setting to disabled, kept the messages and tools, and capped output at 2,048 tokens. No tool was executed during replay, so these responses have no task-success score.

[Every request](rows.tsv) · [Paired differences and output checks](results.json)

## Fix

The converter already forwards `enabled` as true. The patch adds the false branch and preserves the fallback for omitted or unknown settings:

```rust
let enabled = match thinking.thinking_type.as_str() {
    "enabled" => true,
    "disabled" => false,
    _ => return None,
};
```

The [patch](thinking-disabled.patch) includes four regressions. Two fail on stock; all four pass with the fix. The converter checks use native protocol types. Separate full-library Linux builds passed 48 HTTP controls across four deployment configurations and 16 exact-prompt checks over the study inputs. Patched Messages with thinking disabled matches Chat Completions with `enable_thinking: false` token for token.

[PR #8485](https://github.com/ai-dynamo/dynamo/pull/8485) addresses how generated reasoning appears in Anthropic responses. Its inspected diff leaves this input conversion unchanged.

## Setup

One A100-SXM4-80GB, `Qwen/Qwen3-8B`, BF16, TP=1, 32,768 context and 16-token cache blocks. The worker image contains Dynamo 1.5.0 and vLLM 0.28.0. Both native frontends were built from `6e4221b` with the same debug profile; only the converter differs. Later source checks found the same converter path.

Both frontends enabled the Anthropic API, preamble stripping and streaming tool dispatch. The worker used the Qwen3 reasoning and Hermes tool parsers, with thinking enabled by default. [Exact versions, hashes and commands](manifest.json).

Pairs used identical request bodies, temperature 0 and a 2,048-token output limit. Their order alternated. Each physical request had a unique cache salt and zero cached tokens. Four readiness calls are excluded; all 48 measured calls remain, including the six limit hits. There were no HTTP errors or timeouts.

Native generated-token IDs agree with final API usage on every call. The analyzer also checks prompt IDs against the saved CPU renders. Its `first_output_s` field can include whitespace text; complete valid tool timing is recorded separately.

## Reproduce

From the repository root, with Python 3.12+ and uv:

```sh
uv run --no-project --python 3.12 \
  experiments/dynamo-thinking-20261007/reproduce.py \
  --output /tmp/reedcode-thinking-reproduced
```

This verifies the hashes, extracts the saved request records, recomputes every row and checks the result against the published tables. No GPU or API key is needed.

```sh
uv run --no-project --python 3.12 --with jsonschema==4.25.1 python \
  -m unittest discover -s experiments/dynamo-thinking-20261007 -p 'test_analyze.py'
```

## Limits

This measures explicit disabled requests on one model and a quiet server. Their frequency in real deployments is unmeasured. The four snapshots are correlated, and the response contents change between conditions. The matched debug frontends and cold-cache setup limit latency generalization. The result establishes request-control behavior and its cost on these inputs; task quality and concurrent throughput need separate evaluation.
