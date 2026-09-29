# Stock image source check

The September 28 deployment reported Dynamo commit
`32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`. All 12 files checked at that
revision are byte-for-byte identical to the corresponding files at the v1.5.0
tag, `b83b1d9304ebfc624709ac46db32b1b6f1ff1615`.

The [manifest](manifest.json) records each public source URL, HTTP status,
SHA-256, byte count, and comparison hash. [commit.json](commit.json) is the
GitHub commit response. The full source files are under [image/](image/).

This verifies these source-level facts for the image-reported revision:

| Check | Source and line |
| --- | --- |
| Anthropic endpoint requires `--enable-anthropic-api`; default is off | `components/src/dynamo/frontend/frontend_args.py:537` |
| `--strip-anthropic-preamble` defaults to off | `components/src/dynamo/frontend/frontend_args.py:547` |
| Responses endpoint defaults to on | `lib/llm/src/http/service/service_v2.rs:676` |
| Anthropic handler enables thinking when a reasoning parser is configured and the request has not disabled it | `lib/llm/src/http/service/anthropic.rs:507` |
| Thinking accompanying tool calls becomes reasoning segments | `lib/llm/src/protocols/anthropic/types.rs:436` |
| Streamed thinking closes with an `erased` signature | `lib/llm/src/protocols/anthropic/stream_converter.rs:450` |
| Final Anthropic input usage separates uncached and cached tokens | `lib/llm/src/protocols/anthropic/types.rs:532` |
| TRACE logs include the complete preprocessed request | `lib/llm/src/preprocessor.rs:6987` |
| That request derives `Debug` and contains `token_ids` | `lib/llm/src/protocols/common/preprocessor.rs:242` |
| Worker ingress can log the decoded request | `lib/runtime/src/pipeline/network/ingress/push_handler.rs:424` |

For the capture smoke test, use `DYN_LOGGING_CONSOLE_FORMAT=jsonl` and
`DYN_LOG='info,dynamo_llm::preprocessor=trace'`. Full IDs come from the
preprocessor trace. `request_end` contains block hashes, while
`request_payload` contains the normalized chat contract. Save the original
harness body separately through the transparent proxy.

The image still needs a runtime smoke test to check its enabled features,
complete log capture, request linkage, and the real CLI's thinking/tool round
trip. These file comparisons do not verify the complete container contents.

Reproduce with a checkout at the pinned tag:

```bash
python3 experiments/dynamo-upstream/parity-run-1/source-check/fetch_source.py \
  --tag-checkout /tmp/reedcode-dynamo-v1.5.0
```

The script makes unauthenticated public GitHub requests and refreshes the
source files and manifest. It reads the comparison files from the Git commit,
so local edits in the comparison checkout cannot change their hashes.
