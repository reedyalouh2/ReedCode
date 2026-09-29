# Report input and interpretation

`report.py` consumes an explicit request mapping. It reads saved SSE and backend input-ID arrays; it never renders a replacement prompt or assumes that two request-ID strings mean the same request.

```sh
python3 experiments/dynamo-upstream/parity-run-1/report.py \
  --mapping /path/to/study/mapping.json --output /path/to/new-report

python3 -m unittest discover \
  -s experiments/dynamo-upstream/parity-run-1 -p 'test_report.py' -v
```

The following is a schema example. Its IDs and filenames are placeholders, not collected evidence. Paths are relative to the mapping file. Requests must appear in submission order.

```json
{
  "block_size": 16,
  "sessions": [{
    "id": "claude-session",
    "protocol": "anthropic",
    "requests": [{
      "id": "client-0001",
      "conversation_id": "main",
      "user_turn": 1,
      "capture": "capture/0001",
      "backend": {
        "path": "backend/request-23.json",
        "sha256": "<SHA-256 of the full file>",
        "token_ids_pointer": "/token_ids",
        "request_id": "backend-23",
        "worker_id": "worker-1",
        "epoch": "process-start-1"
      },
      "link_evidence": ["controller-ledger.json: explicit client/backend linkage"]
    }]
  }]
}
```

Use protocol `responses` for Codex. The capture directory must contain the recorder's `metadata.json`, `request.body`, and `response.body`. Backend files use a JSON pointer to an integer array; an empty pointer selects a top-level array. Keep ancillary inference as rows with `auxiliary: true` and its own `conversation_id`. It has a separate aggregate and cannot enter the main conversation's prefix reference.

The parser checks file hashes, response completion, final usage and backend input length. It takes the maximum LCP with earlier completed requests in the same conversation, worker and process epoch, then rounds down to a 16-token block. Earlier requests that overlap the current request in the capture clock are excluded. Keep the recorder on the same monotonic clock throughout a session.

`join_wire.py` builds this mapping from the recorder's observer header and `decode.py`'s `http_link`. Decode the downloaded PCAP with absolute local paths for `--frontend-log`; the join checks those raw logs against their saved hashes and exact linkage rows. Pass each controller output directory, containing `result.json` and `capture/`:

```sh
python3 experiments/dynamo-upstream/parity-run-1/join_wire.py \
  --wire /path/to/study/wire.json \
  --claude /path/to/study/claude --codex /path/to/study/codex \
  --worker-id '<recorded worker ID>' --epoch '<recorded process epoch>' \
  --identity-evidence /path/to/study/capture-proof.json \
  --main-conversation-reviewed --output /path/to/study/mapping.json
```

Before using `--main-conversation-reviewed`, inspect the inference bodies for side requests. If any belong to another conversation, supply `--labels labels.json`. Its keys are capture IDs such as `claude-0003`; each value supplies `conversation_id`, `auxiliary: true`, and an `evidence` list explaining the classification. Different worker epochs require separate invocations. User turns come from the controller's recorded time intervals. Missing request links stay visible as excluded rows. The helper attaches backend usage only when the mapped response stream supplies explicit input and cache counts that agree throughout the response.

Each row reports input, cached and ideal tokens; actual/ideal reuse; the signed gap; warnings; and the IDs used as references. Aggregates use token sums over the same usable rows. Unknown usage and broken links remain visible as excluded rows. A final output-limit response with usage remains included. Failed and unfinished streams cannot provide a completed reference.

## Usage and engine boundary

Responses total input already includes cache reads. Anthropic total input is final uncached input plus cache reads plus cache writes. The parser ignores the initial Anthropic estimate. Stock Dynamo omits `cache_read_input_tokens` both for zero and for unknown backend usage, so a missing field remains unknown.

If an independently captured backend usage record resolves that ambiguity, attach it explicitly to the request:

```json
"backend_usage": {
  "input_tokens": 320,
  "cached_tokens": 0,
  "evidence": ["backend-final-usage.json: mapped request-23"]
}
```

The parser checks consistency against any frontend count and the input-ID array. The original frontend usage stays in the report. Do not populate this field from a guess about missing values.

The optional top-level `engine_boundary` accepts `{"rule": "last_token_recomputed", "evidence": ["<pinned engine source>"]}`. Once verified for the deployed backend, this produces a separate eligible ideal capped at the full blocks before the last input token. Without that evidence the eligible ideal stays unknown. The input-only ideal is always retained.

## Causes

Add a top-level `causes` object keyed by the client request ID. Its `compatible_misses` entries cover token offsets in `[cached_tokens, eligible_ideal_tokens)`, or the input-only ideal when the engine rule is unverified:

```json
"causes": {
  "client-0002": {
    "compatible_misses": [{
      "start": 32,
      "end": 48,
      "cause": "eviction",
      "explanation": "The matching block chain was stored, then removed before lookup.",
      "evidence": ["kv-events.jsonl: event IDs and ancestry"]
    }]
  }
}
```

Ranges may not overlap or extend beyond the measured miss interval. Every attribution needs an explanation and evidence. Gaps are filled with `other` and an explicit missing-evidence explanation. Supported labels are `rendering`, `reasoning_round_trip`, `tool_call_round_trip`, `tool_schema_order`, `harness_edits`, `eviction`, `engine_boundary`, and `other`.

`history_rewrites` uses the same range structure plus `reference_request_id`, identifying an earlier input in the conversation. These counts are supplied diagnostic exposures; the baseline parser cannot discover them from token arrays alone. They have a separate ranking and are never added to compatible-miss counts. No supplied rewrite ledger means the rewrite exposure is unmeasured.

The tests use hand-checked token arrays, actual saved CLI stub streams, and SSE from the pinned stock frontend CPU check. Those CPU responses contain synthetic usage and generation. They validate parsing, not cache performance. The script produces JSON and Markdown and leaves the final parity verdict to the full evidence review.
