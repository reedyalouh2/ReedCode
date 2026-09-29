# GPU readiness attempt

The pod started, but the pinned image ran as UID 1000 with no effective capabilities. `tcpdump` was absent. This failed the required packet-capture gate, so I deleted the pod before downloading Qwen or sending a model request.

| Check | Result |
|---|---|
| Requested GPU | One A100 SXM4 80GB at $1.59/hour |
| Container user | UID 1000 |
| Effective capabilities | `0000000000000000` |
| Packet capture executable | Absent |
| Model requests | 0 |
| Stock-vs-fix measurements | Unmeasured |
| Claude Code / Codex parity | Unmeasured |
| Cleanup | Pod absent; active spend $0/hour |

The pod existed for 6.08 minutes from the recorded create time to confirmed deletion. Charging that entire period, including temporary disk at the reserved rate, gives an estimate of $0.163. The account balance was unchanged at the first post-deletion check; final billing was not confirmed.

[Raw identity check](identity.stdout), [command](identity-command.json), [startup logs](startup-container-logs.jsonl), [cleanup](controller-cleanup.jsonl), and [result](result.json) are saved here. The image digest is in `state.json`. No replacement pod was created.

This attempt only establishes a deployment limitation in this Runpod container. It provides no evidence about speculative-prefill performance or cache-reuse parity.
