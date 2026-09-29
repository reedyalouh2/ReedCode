# Stock runtime token capture

Passive TCP capture recovers the actual request token IDs received by the stock CPU worker. It also recovers the token IDs the worker returns, with request-to-response linkage from Dynamo's control ID and response-stream subject. No Dynamo code, `nvext`, or request body was changed for this capture.

| Stock API route | Input tokens, request 1 | Input tokens, request 2 | Exact callback match | Kernel drops |
| --- | ---: | ---: | --- | ---: |
| Codex `/v1/responses` | 8,696 | 8,869 | 2/2 | 0 |
| Claude `/v1/messages` | 19,975 | 20,111 | 2/2 | 0 |

Both use the shipped stock 1.5.0 wheels from the pinned image, revision `32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653`. The [Linux readiness packet](../../001-speculative-prefill/linux-readiness/README.md) records the image, wheel and tokenizer hashes. A CPU callback saved each received payload and returned `[151668, 198, 151645]`. Those exact returned IDs appeared in all eight decoded response streams: two requests per API, repeated with an observer header. No model or GPU was loaded.

## Reproduce the evidence check

```bash
wire=experiments/dynamo-upstream/parity-run-1/wire-capture
uv run --with msgpack==1.1.1 --with xxhash==3.5.0 python "$wire/check.py"
```

This verifies the archived evidence, decodes all four successful captures again, compares all request fields with the worker callback, and runs 12 tests. It saves [results.json](results.json) and [tests.log](tests.log). `evidence.tar.gz` contains the PCAP files, callbacks, logs and HTTP responses; [evidence.json](evidence.json) hashes each member. The pinned [runtime source](source.json) explains the framing and codecs.

## Make a new CPU capture

The observer image adds tcpdump to the existing CPU build image. The frontend and runtime still come from the shipped wheels. These commands use the prepared files and virtual environment under `/tmp/reedcode-linux-readiness`, as described in the Linux readiness packet.

```bash
docker build --platform linux/amd64 -t reedcode-wire-capture:1.96.1 \
  -f "$wire/Dockerfile" "$wire"

docker run --rm --platform linux/amd64 --network none \
  --mount type=bind,source=/tmp/reedcode-linux-readiness,target=/work \
  --mount type=bind,source="$PWD/$wire",target=/capture,readonly \
  -e LD_LIBRARY_PATH=/work/backend-ffmpeg/usr/local/lib \
  -e PROTOCOL_OUTPUT=/work/wire-codex-new -e PROTOCOL_CLIENT=codex \
  -e WIRE_CAPTURE_ISOLATED=network-none -e WIRE_REQUEST_IDS=1 \
  reedcode-wire-capture:1.96.1 \
  /work/venvs/backend/bin/python /capture/run_capture.py
```

Use `PROTOCOL_CLIENT=claude` and a fresh output directory for the other route. Capture is restricted to `lo`, with Docker networking disabled. The filter excludes the public HTTP port and keeps the internal TCP request/response traffic. `--immediate-mode`, full packet snapshots and a 4 MiB buffer keep short exchanges from being left in libpcap's buffer.

Decode a fresh capture:

```bash
uv run --with msgpack==1.1.1 --with xxhash==3.5.0 python "$wire/decode.py" \
  /tmp/reedcode-linux-readiness/wire-codex-new/runtime.pcap \
  --callbacks /tmp/reedcode-linux-readiness/wire-codex-new \
  --frontend-log /tmp/reedcode-linux-readiness/wire-codex-new/frontend.log \
  --output /tmp/reedcode-linux-readiness/wire-codex-new/decoded.json
```

The GPU pod routes traffic to its own interface address through `lo`. For that layout, pass `--local-address` with the address recorded from the pod. The decoder accepts it only when both packet addresses match and either TCP port is 20000 or 20003. The default remains loopback addresses only. Keep the interface observation with the capture evidence.

The first attempt omitted immediate delivery. It finished the two CPU requests, but tcpdump reported 0 captured packets and 90 received by its filter. That failed PCAP and the original counters remain in the archive. The first retry was rejected by automatic approval review because the account had reached its usage limit. After the approval service recovered, the same isolated command was approved and completed. No permissions were weakened to get around that rejection.

## What the decoder checks

It reconstructs each TCP direction from captured sequence numbers, removes identical retransmissions, and accepts packet reordering and sequence wrap. It rejects missing SYNs, byte gaps, conflicting retransmissions, truncated packets, reused connection tuples and unsupported traffic. Dynamo frame lengths and nonzero xxHash checksums are checked before JSON or MessagePack decoding.

The request control ID links to the response-stream subject. Each model response needs its prologue, final marker and sentinel. A captured `clear_kv_blocks` request may have its helper response outside the selected ports; the decoder keeps it explicitly incomplete and never treats it as model evidence. Dynamo's `worker_kv_query_source_<hex>` recovery requests are also retained as control traffic, including their plain response payloads. Model token arrays come from the runtime's `Annotated` response payload. Tokens are never reconstructed by rendering text or running a tokenizer.

## Limits before the GPU study

The CPU proof covers plaintext TCP, unary requests, and the exact stock runtime revision above. TLS, fragmented IP, TCP connection reuse across new epochs, request streaming, and other transports need separate validation. Incomplete traffic produces an error and leaves the raw capture for inspection.

The HTTP-to-runtime join is verified on both APIs. Adding `X-Request-ID: wire-{client}-{index}` left all four input arrays unchanged. Stock frontend JSON rows with `message: "request received"` contain both `x_request_id` and internal `request_id`. That internal ID equals the wire control ID for all four requests. The header is echoed in HTTP responses but absent from the TCP metadata itself.

`decode.py --frontend-log PATH` saves the log hash and exact bridge row for each match. Repeat the option for frontend restarts. Duplicate observer IDs or internal IDs fail the join. Unlinked model requests stay listed; a report must account for them before claiming complete capture. The live observer preserves an existing client header and only adds a unique value when absent. Request bodies remain byte-for-byte unchanged.

For the approved single-pod layout, the passive filter is `tcp and (port 20000 or port 20003)`: backend request ingress plus frontend responses. Start capture before those processes open TCP connections, stop it after the final response, and retain tcpdump's zero-drop counters. Reset-helper responses use a separate port and are verified by their saved helper output and live clear-event proof.

For the live study, match the expected request count, confirm zero capture drops, and preserve the complete interval. A parser cannot detect an entire missing connection by examining only the connections it received. This CPU test compares against every callback to close that gap. GPU runs must also confirm that the unmodified vLLM worker receives the same wire payload and that event capture covers the same process epoch. Capture overhead remains unmeasured.
