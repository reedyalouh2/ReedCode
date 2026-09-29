# The four-token boundary

Every one of the 42 archived tool continuations first differs at **the preceding input's length minus four**. Those four input tokens are always the same:

| Token ID | Decoded text |
| --- | --- |
| 151667 | `<think>` |
| 271 | `\n\n` |
| 151668 | `</think>` |
| 271 | `\n\n` |

The generation prefix ends with `<|im_start|>assistant\n<think>\n\n</think>\n\n`. In the next request, the same assistant header is followed directly by a tool call in 15 cases and assistant prose in 27. None of these messages contains nonempty reasoning.

All preceding input tokens before that suffix still match. This is why the [trajectory survey](trajectory-survey.md) finds very little loss of earlier input, while the completed assistant message still needs a different prefix. The change occurs before sampled output, so later decoded tokens cannot extend the original request's matching prefix. Their raw token IDs were not recorded.

## Block alignment

Removing four tokens crosses a complete 16-token block boundary in five cases:

| Prior input length modulo 16 | Continuations | Complete-block tokens lost per continuation |
| --- | ---: | ---: |
| 0 | 2 | 16 |
| 2 | 2 | 16 |
| 3 | 1 | 16 |
| 4–15 | 37 | 0 |

There are no inputs with remainder 1 in this sample. The five affected transitions are `r01_off` turn 3, `r02_on` turn 4, `r03_off` turn 3, and `r05_off` turns 3 and 4. Their full run IDs and zero-based mismatch positions are in [boundary-detail.json](boundary-detail.json).

## What the template says about thinking

The pinned template's `enable_thinking` setting changes the new generation prefix. With thinking disabled, it appends the empty four-token block above. With thinking enabled, it leaves the assistant header open for generation.

Historical reasoning follows a separate rule. The template finds the most recent ordinary user message. Tool responses leave that position unchanged, so nonempty reasoning from assistants after that user stays in the rendered history. A new ordinary user moves the position forward, causing earlier assistant reasoning to be omitted. A user message wrapped in `<tool_response>` tags is excluded from this scan.

These are source-level rules. The recorded pilot has no nonempty reasoning or new ordinary-user turns to measure the larger rewrite. It only demonstrates removal of the empty generation suffix during thinking-disabled tool continuations.

## Reproduce

```bash
uv run --with-requirements experiments/dynamo-prefix/requirements.txt \
  python experiments/dynamo-prefix/boundary_detail.py \
  --tokenizer /tmp/reedcode-prefix-tokenizer/tokenizer.json \
  --output /tmp/boundary-detail.json
```

The script checks the archive, joins all 52 inputs to their server records, and verifies 9,703 input hashes before comparing boundaries. Its windows are local reconstructions checked against those saved fingerprints. It makes no model calls and measures no cache residency, saved compute, or latency change.
