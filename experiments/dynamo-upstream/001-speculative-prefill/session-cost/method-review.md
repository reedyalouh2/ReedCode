# Review of the session cost estimate

A prepared prompt can disagree with the real continuation and still reuse an earlier prepared prompt. Its incompatible token volume therefore cannot be read as fresh prefill work. Cache pollution requires a further observation: preparation must occupy capacity that displaces useful work or otherwise harms service.

## Quantities to keep separate

For transition `i`, let `N_i` be the real current input, `W_i` the reconstructed stock preparation, `F_i` the actual next input, and `B` the cache block size.

- **Compatibility:** record `c_i = LCP(W_i, F_i)`, the suffix `len(W_i) - c_i`, and the nonmatching complete-block volume `B * (floor(len(W_i) / B) - floor(c_i / B))`. The block containing the first different token also fails prefix matching. These quantities describe compatibility with this continuation.
- **Potential input reuse:** record the LCP with `N_i`, the largest LCP with earlier real inputs, the largest LCP with earlier prepared inputs, and the maximum across those sets. Include the current real input; exclude the future continuation and the current preparation itself.
- **Counterfactual unmatched blocks:** subtract the matching full-block prefix from `B * floor(len(W_i) / B)`. Compare the real-input-only history with a history that also contains earlier preparations. Label both as input-only models with no eviction, completed prior work, and a common cache identity.
- **Unmeasured work:** retain the partial-block length and report GPU prefill, occupancy, eviction, and latency as unknown unless collected directly. Check the backend's last-token and block-publication rules before predicting reported cache hits.

The history model starts cold within each session. The recorded server carried cache across sessions, and a live server can evict, cancel, or overlap preparations. Those differences can move actual reuse in either direction. Consequently the counterfactual is not a lower bound on measured GPU cost. A token count also cannot be converted directly into GPU time.

Raw generated token IDs were not saved. If the LCP stops inside a previous input, later generated IDs cannot repair that mismatch. Otherwise retain an interval bounded by the previous input and recorded output length. One generated token can also complete a partial block; input-only accounting leaves that possibility out.

## Independent arithmetic check

I checked all 52 real request inputs against the archived server fingerprints, then reconstructed preparations for the 42 observed continuations across ten sessions:

| Quantity | Tokens |
| --- | ---: |
| Total reconstructed preparation inputs | 93,085 |
| Suffix after the first mismatch with the actual next input | 91,783 |
| Nonmatching complete blocks, expressed as tokens | 92,048 |
| Counterfactual unmatched complete blocks after allowing earlier preparations | 32,368 |

Every preparation-to-continuation LCP is 31 tokens. Earlier preparations offer a longer matching prefix than any real input on 32 of 42 transitions. This is sufficient to show why counting every incompatible token as newly computed would overstate the input-only estimate.

Prepared inputs range from 93 to 3,268 tokens in these transitions. The archive contains no 50K-token example. Ten terminal preparations total another 49,683 tokens, but they have no observed continuation. Keep them separate from transition compatibility. Only 20 of the 42 transitions came from source-on runs; applying the stock builder to source-off runs is a counterfactual. These prepared sequences are reconstructions, not per-preparation GPU measurements.

For a constructed long-context case, record where the added text sits. Qwen inserts tools after the first system content: long system text can precede the divergence, while later user/tool history follows it. Total prompt length alone does not determine the incompatible suffix.

The earlier replay's roughly 7.305 versus 7.266 seconds is a small point-estimate difference from five pairs, with no confidence interval and unavailable restart-safe server comparisons. It does not establish a systematic benefit or penalty. A claim about harmful extra work needs a controlled serving experiment with attributable preparation requests, cache misses, eviction or residency evidence, and an unchanged workload.
