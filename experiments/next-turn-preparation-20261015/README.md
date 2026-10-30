# Preparing the next user turn

I tested whether a coding harness could prepare useful KV cache before receiving the next user message. Across four fresh sessions, the controller supplied **9,024 additional cached tokens across eight follow-ups**. The four sessions with preparation off received zero preparation credit.

The largest boundary contributed **5,968 tokens**, or 66.1% of the gain. That request reused 12,224 of 12,777 input tokens (95.7%). Earlier foreground history could explain at most 6,256 tokens (49.0%). It followed a call-limited turn in a session that began with an empty reply.

## How it works

Qwen3's template removes earlier reasoning when a new ordinary user message arrives. The resulting history can differ from the prefix already in cache. After each valid assistant response, the controller renders that rewritten history and prepares its complete 16-token blocks. It stops before unknown user content. The predictor receives only the preceding request and completed assistant response.

Preparation runs alongside tool work. An arriving foreground request cancels unfinished preparation without waiting for its cleanup. The real request then uses whichever matching blocks are available. [The controller](source/controller.py), [pinned renderer](source/prepare.py) and [capture code](source/capture_worker.py) are included.

## Results

Stock Dynamo 1.5.0, vLLM 0.28.0 and Qwen3-8B ran on one A100 SXM 80GB, with thinking enabled, BF16, prefix caching and a 32,768-token context limit. [runtime.json](runtime.json) records the image digest, model revision, commands, parsers and capture settings. The commands describe the original launch; CPU replay uses the saved captures. [Startup output](runtime-startup.log) records KV capacity.

The target was a frozen Requests bug-fix task followed by two additional user messages. Four trials ran alone; four ran alongside a frozen Flask task. Each trial started with a verified cache reset. The eight target sessions and four background sessions produced 91 foreground model calls. All requests, tools and responses were generated during the run. [tasks.json](tasks.json) contains the exact inputs and source commits.

| Pair | Additional cached tokens with preparation | Uncached input in completed preparations | Target TTFT p90, off / preparation |
| --- | ---: | ---: | ---: |
| Quiet 0 | 1,200 | 1,968 | 75.7 / 72.3 ms |
| Quiet 1 | 1,488 | 2,672 | 83.5 / 87.3 ms |
| Background 0 | 336 | 1,408 | 490.8 / 89.2 ms |
| Background 1 | 6,000 | 2,608 + unknown canceled work | 86.7 / 462.3 ms |

These p90 values cover all target requests. For the four quiet-server follow-ups, native TTFT ranged from 26.36–39.39 ms with preparation and 54.56–76.32 ms off.

Credit requires backend cache hits above a conservative ceiling from earlier foreground inputs and outputs, including the background session. A matching preparation must have finished before the real request entered the backend. Token parity, request IDs, cache identities and capture completeness passed for every trial. A separate calculation checks all 104 foreground requests across both cohorts.

Of 31 preparation attempts, 23 completed, five were canceled before starting, two were ineligible empty replies, and one was canceled after dispatch. Nineteen of 25 eligible attempts with a later request finished before its dispatch. Four other completed preparations had no later request.

Completed preparations consumed 8,656 uncached prompt tokens and generated 23 discarded samples. The canceled request had an 11,104-token prompt and unknown partial compute; later preparation reused part of its prefix. Total preparation cost and net savings remain unknown. Completed preparations also contained 232 distinct prefix blocks, or 3,712 tokens, with no later matching real input. Those counts do not measure physical KV occupancy.

## Failures and cohorts

The main cohort retained four empty replies and four model timeouts. One target and three background sessions hit the 60-second request deadline. There were no output-limit hits. Empty replies ended their turns as failures; the next frozen follow-up was still sent. They triggered neither tools nor preparation.

The original three-trial cohort is reported separately. Its first off/on pair stopped at empty replies before reaching a follow-up. The third trial reached both follow-ups before a researcher stop, with 688 additional cached tokens on its completed follow-up and 464 on its interrupted one. That partial 1,152-token result is excluded from the headline.

The amendment changed only handling of completed empty replies. Settings and inputs stayed fixed. [original-cohort.patch](source/original-cohort.patch) restores the earlier controller. Readiness qualification exceeded the planned 25-minute window; the measurement and shutdown deadlines stayed unchanged.

## Limits

There are two pairs per load, trajectories differ, and one boundary dominates the result. Background execution overlapped 52.5% and 95.7% of target execution in off trials, versus 96.5% and 87.6% with preparation. Latency and interference therefore remain unresolved. The server had 339,824 tokens of KV capacity; this run does not establish behavior under cache pressure. Task success and performance on other templates were not established. Timings include capture instrumentation, whose overhead was not measured separately.

## Reproduce

From the repository root, with Python 3.12+ and uv:

```sh
uv run --script experiments/next-turn-preparation-20261015/reproduce.py --test
```

This installs the pinned CPU dependencies, verifies and extracts the evidence, recomputes both cohorts, runs the independent credit and overlap checks, then runs 72 regression tests. It makes no model calls. Reports go to `.reproduction/results/` inside this directory. [requests.tsv](results/requests.tsv) lists all 104 foreground requests with input, cached, preparation-credit and native-reported output-token counts, TTFT and recorded outcomes. Blank cells mean unavailable; turn and session outcomes describe control flow. The full [original](results/original.json.gz) and [amended](results/amended.json.gz) reports are compressed without changing their JSON bytes. CPU reproduction regenerates plain JSON and the same TSV. [Storage hashes](results/report-storage.json) connect the compressed files to the uncompressed report hashes used by the independent audits.

[evidence.tar.gz](evidence.tar.gz) contains the full closed backend capture, KV events, pinned tokenizer and both client cohorts. The worker recorded 126,477 contiguous events with no dropped or pending records. [evidence.json](evidence.json) describes the archive and the excluded server logs, metrics and deployment files. Only local `capture_file` metadata paths were generalized; measured requests, responses, tokens and timings are unchanged. [File hashes](evidence-files.json) include original hashes for those metadata edits. [Source provenance](source-provenance.json) records the standalone packaging changes.
