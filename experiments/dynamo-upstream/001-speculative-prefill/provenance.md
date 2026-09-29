# Provenance of the stock-hint reproduction

The isolated checks in the September 28 pilot exercised Dynamo's stock `nvext.agent_hints.speculative_prefill` path. The client made ordinary streamed `/v1/chat/completions` requests. It never submitted a prepared token prefix. The separate [corrected-prefix GPU check](../../dynamo-prefix/gpu-20260928/README.md) later used an explicit adapter through `/v1/completions`.

## Original records

[provenance-evidence.tar.gz](provenance-evidence.tar.gz) contains the four isolated conversations, all 16 metric snapshots, launch scripts, runtime versions, the captured source file, the complete frontend debug log, and excerpts of backend and HTTP request traces. Full members are byte-identical to the original archive. Excerpts preserve the original line bytes; [provenance-manifest.json](provenance-manifest.json) records their source hashes and one-based line numbers.

- Original archive: `experiments/dynamo-20260928/raw-records.tar.gz`
- Original archive SHA-256: `4a4650d97c06173e817e21657a651daa7d9d1be20441db92869fb42a584a1cb1`
- Extracted evidence SHA-256: `f0d1ca0a65f6b10d729393735baaab29c13e4ae8a85fd60c711de79ec33026ed`

Every member listed in the original checksum manifest was verified before extraction. None of the original records was edited.

The deployment was Dynamo 1.5.0, image commit `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`, vLLM 0.28.0, and Qwen/Qwen3-8B revision `b968826d9c46dd6066d109eabc6255188de91218`, on an A100 80GB. The image digest, package output, and launch command are in the bundle. Thinking was disabled; prefix caching, KV events, and KV routing were enabled.

## What the client sent

`setup/check_prefixes.py` loops over text/tool and off/on conditions. Each conversation gets a new session ID and a random identifier in the system message. It sends two normal chat requests, passing the hint through `extra_body`. Between requests it waits two seconds and records metrics.

The compact client JSON stores the base request before `extra_body` is merged. Its lack of an `nvext` field therefore cannot establish whether the hint was sent. The server's `request_payload` records contain the complete received request. All four on requests contain `"nvext":{"agent_hints":{"speculative_prefill":true}}`; all four off requests contain `false`. The tool requests also contain the `record_value` schema and the real follow-up includes its assistant tool call.

## The tool-call chain

All times below are September 28, 2026 UTC. Line references use the original archived files.

1. Client request `59fd1e70-5c6a-48a3-9642-a2305cb37fee` arrives with the stock hint enabled. `server/frontend-trace.jsonl:625` records the request body and tool schema. The initial prompt has 202 tokens and zero cached tokens.
2. At `09:49:24.979503`, `server/frontend.log:106` records `Speculative prefill: sending next-turn prefix` from `preprocessor/speculative_prefill.rs`, with 72 tokens.
3. The next line records the router's four full 16-token local block hashes. The following worker-selection line identifies the internal request as `7e0604ef-aead-454d-bf62-3172907a2c1f`. The warm request's hash-log row itself has no request ID, so this association uses the isolated sequence and adjacent worker-selection record.
4. `server/backend.log:1085–1086` records that internal request arriving at the backend `generate` endpoint and completing at `09:49:25.002849`. `server/frontend.log:112` records its successful route completion with the same ID.
5. The real follow-up, `d5a3f0ff-0cdf-407a-ae67-207807687e79`, arrives at `09:49:27.380484`. It has 244 input tokens and reports 192 cached tokens. Its router hashes appear at `server/frontend.log:115`.

The first two warmed blocks match the real follow-up. The third differs:

| Zero-based block | Stock warmed local hash | Real follow-up local hash |
| --- | ---: | ---: |
| 0 | 4557685901235297031 | 4557685901235297031 |
| 1 | 8484308257205712445 | 8484308257205712445 |
| 2 | 1825881976636037031 | 8393990704234532549 |
| 3 | 15261822079647575809 | 8922630584695415248 |

These runtime hashes establish a divergence within tokens 32–47. They are independent local block hashes, rather than the rolling hashes in `request_end.replay`. The local CPU reconstruction places the first difference at token index **40**, and matches all four recorded warmed hashes. The 72-token warm prompt's last eight token IDs were not captured or covered by those full-block hashes.

The original 202-token prompt already supplies all 12 full blocks that the real follow-up reuses. The stock preparation adds no compatible full block beyond them in this example. The [matching 1.5.0 source](https://github.com/ai-dynamo/dynamo/blob/b83b1d9304ebfc624709ac46db32b1b6f1ff1615/lib/llm/src/preprocessor/speculative_prefill.rs) carries only messages in its speculative request and accumulates only assistant text. The local reproduction checks how omitting tools and tool calls changes the rendering.

## Text control and cold-prefix checks

The text on request is `d5360dd8-7b9b-4916-a2f2-33279b20c025`; its internal warm request is `24f93c56-30d4-411a-9eff-d0b3699652ed`; its follow-up is `d8c6a27c-cc15-4152-b635-9f82c1750f0a`. The stock module records 64 prepared tokens at `server/frontend.log:64`. All four prepared blocks are hashed. Three match the 78-token follow-up, and the fourth differs. The CPU reconstruction locates the difference at token index **56**.

| Conversation | Initial prompt tokens | Initial cached tokens | Follow-up prompt tokens | Follow-up cached tokens |
| --- | ---: | ---: | ---: | ---: |
| Text off | 59 | 0 | 77 | 48 |
| Text on | 60 | 0 | 78 | 48 |
| Tool off | 200 | 0 | 242 | 192 |
| Tool on | 202 | 0 | 244 | 192 |

The random identifiers have different token lengths. These are separate cold-prefix mechanism checks, not an identical-prompt latency comparison. The server cache was not globally reset. Every first request reported zero cache hits, and the relevant on-condition block comparisons use that same conversation's original and follow-up requests.

The earlier `tool-check/` smoke test reused the same conversation prefix across conditions. It is excluded from this cold-prefix claim.

## Counter corroboration

All eight before/after intervals have zero running and waiting requests at both snapshots. Each off request increases the backend completion counter by one. Each on request increases it by two, and its generation counter includes one token beyond the client response.

For the first tool on call, the reported prompt counter increases by `274 = 202 + 72` and generation by `21 = 20 + 1`. For the first text on call, these increases are `124 = 60 + 64` and `4 = 3 + 1`. The extra preparation also occurs after the second on request, outside the two-turn reuse comparison. All eight intervals are summarized in the manifest.

These counters support the request-log evidence. They do not establish GPU work: prompt totals include cached tokens. The endpoint omitted `process_start_time_seconds`, so this dataset's automatic restart validation was unavailable. No latency effect or valid whole-study phase-counter comparison is claimed here.

## What was observed

The archived server records show stock-hint requests, the stock module scheduling extra preparation, runtime input lengths and full-block hashes at the frontend router, and backend receipt/completion of the extra requests. The cache counts show no added follow-up hits in these two isolated examples.

The archive does **not** contain raw speculative token IDs at the backend, raw sampled output IDs, or a corrected stock-server run. Exact mismatch token positions come from local reconstruction checked against the available runtime hashes. The later adapter run demonstrates cache reuse for explicitly supplied corrected token IDs; it does not validate an upstream implementation change.
